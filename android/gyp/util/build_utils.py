# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import collections
import contextlib
import filecmp
import fnmatch
import json
import os
import pipes
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from util import md5_check

sys.path.append(os.path.join(os.path.dirname(__file__),
                             os.pardir, os.pardir, os.pardir))
import gn_helpers

# Definition copied from pylib/constants/__init__.py to avoid adding
# a dependency on pylib.
DIR_SOURCE_ROOT = os.environ.get('CHECKOUT_SOURCE_ROOT',
    os.path.abspath(os.path.join(os.path.dirname(__file__),
                                 os.pardir, os.pardir, os.pardir, os.pardir)))

CHROMIUM_SRC = os.path.normpath(
    os.path.join(os.path.dirname(__file__),
                 os.pardir, os.pardir, os.pardir, os.pardir))
COLORAMA_ROOT = os.path.join(CHROMIUM_SRC,
                             'third_party', 'colorama', 'src')
# aapt should ignore OWNERS files in addition the default ignore pattern.
AAPT_IGNORE_PATTERN = ('!OWNERS:!.svn:!.git:!.ds_store:!*.scc:.*:<dir>_*:' +
                       '!CVS:!thumbs.db:!picasa.ini:!*~:!*.d.stamp')

HERMETIC_TIMESTAMP = (2001, 1, 1, 0, 0, 0)
_HERMETIC_FILE_ATTR = (0o644 << 16)



@contextlib.contextmanager
def TempDir():
  dirname = tempfile.mkdtemp()
  try:
    yield dirname
  finally:
    shutil.rmtree(dirname)


def MakeDirectory(dir_path):
  try:
    os.makedirs(dir_path)
  except OSError:
    pass


def DeleteDirectory(dir_path):
  if os.path.exists(dir_path):
    shutil.rmtree(dir_path)


def Touch(path, fail_if_missing=False):
  if fail_if_missing and not os.path.exists(path):
    raise Exception(path + ' doesn\'t exist.')

  MakeDirectory(os.path.dirname(path))
  with open(path, 'a'):
    os.utime(path, None)


def FindInDirectory(directory, filename_filter):
  files = []
  for root, _dirnames, filenames in os.walk(directory):
    matched_files = fnmatch.filter(filenames, filename_filter)
    files.extend((os.path.join(root, f) for f in matched_files))
  return files


def FindInDirectories(directories, filename_filter):
  all_files = []
  for directory in directories:
    all_files.extend(FindInDirectory(directory, filename_filter))
  return all_files


def ReadBuildVars(path):
  """Parses a build_vars.txt into a dict."""
  with open(path) as f:
    return dict(l.rstrip().split('=', 1) for l in f)


def ParseGnList(value):
  """Converts a "GN-list" command-line parameter into a list.

  Conversions handled:
    * None -> []
    * '' -> []
    * 'asdf' -> ['asdf']
    * '["a", "b"]' -> ['a', 'b']
    * ['["a", "b"]', 'c'] -> ['a', 'b', 'c']  (flattened list)

  The common use for this behavior is in the Android build where things can
  take lists of @FileArg references that are expanded via ExpandFileArgs.
  """
  # Convert None to [].
  if not value:
    return []
  # Convert a list of GN lists to a flattened list.
  if isinstance(value, list):
    ret = []
    for arg in value:
      ret.extend(ParseGnList(arg))
    return ret
  # Convert normal GN list.
  if value.startswith('['):
    return gn_helpers.GNValueParser(value).ParseList()
  # Convert a single string value to a list.
  return [value]


def ParseGypList(gyp_string):
  # The ninja generator doesn't support $ in strings, so use ## to
  # represent $.
  # TODO(cjhopman): Remove when
  # https://code.google.com/p/gyp/issues/detail?id=327
  # is addressed.
  gyp_string = gyp_string.replace('##', '$')

  if gyp_string.startswith('['):
    return ParseGnList(gyp_string)
  return shlex.split(gyp_string)


def CheckOptions(options, parser, required=None):
  if not required:
    return
  for option_name in required:
    if getattr(options, option_name) is None:
      parser.error('--%s is required' % option_name.replace('_', '-'))


def WriteJson(obj, path, only_if_changed=False):
  old_dump = None
  if os.path.exists(path):
    with open(path, 'r') as oldfile:
      old_dump = oldfile.read()

  new_dump = json.dumps(obj, sort_keys=True, indent=2, separators=(',', ': '))

  if not only_if_changed or old_dump != new_dump:
    with open(path, 'w') as outfile:
      outfile.write(new_dump)


def ReadJson(path):
  with open(path, 'r') as jsonfile:
    return json.load(jsonfile)


@contextlib.contextmanager
def AtomicOutput(path, only_if_changed=True):
  """Helper to prevent half-written outputs.

  Args:
    path: Path to the final output file, which will be written atomically.
    only_if_changed: If True (the default), do not touch the filesystem
      if the content has not changed.
  Returns:
    A python context manager that yelds a NamedTemporaryFile instance
    that must be used by clients to write the data to. On exit, the
    manager will try to replace the final output file with the
    temporary one if necessary. The temporary file is always destroyed
    on exit.
  Example:
    with build_utils.AtomicOutput(output_path) as tmp_file:
      subprocess.check_call(['prog', '--output', tmp_file.name])
  """
  # Create in same directory to ensure same filesystem when moving.
  with tempfile.NamedTemporaryFile(suffix=os.path.basename(path),
                                   dir=os.path.dirname(path),
                                   delete=False) as f:
    try:
      yield f

      # file should be closed before comparison/move.
      f.close()
      if not (only_if_changed and os.path.exists(path) and
              filecmp.cmp(f.name, path)):
        shutil.move(f.name, path)
    finally:
      if os.path.exists(f.name):
        os.unlink(f.name)


class CalledProcessError(Exception):
  """This exception is raised when the process run by CheckOutput
  exits with a non-zero exit code."""

  def __init__(self, cwd, args, output):
    super(CalledProcessError, self).__init__()
    self.cwd = cwd
    self.args = args
    self.output = output

  def __str__(self):
    # A user should be able to simply copy and paste the command that failed
    # into their shell.
    copyable_command = '( cd {}; {} )'.format(os.path.abspath(self.cwd),
        ' '.join(map(pipes.quote, self.args)))
    return 'Command failed: {}\n{}'.format(copyable_command, self.output)


def FilterLines(output, filter_string):
  """Output filter from build_utils.CheckOutput.

  Args:
    output: Executable output as from build_utils.CheckOutput.
    filter_string: An RE string that will filter (remove) matching
        lines from |output|.

  Returns:
    The filtered output, as a single string.
  """
  re_filter = re.compile(filter_string)
  return '\n'.join(
      line for line in output.splitlines() if not re_filter.search(line))


# This can be used in most cases like subprocess.check_output(). The output,
# particularly when the command fails, better highlights the command's failure.
# If the command fails, raises a build_utils.CalledProcessError.
def CheckOutput(args, cwd=None, env=None,
                print_stdout=False, print_stderr=True,
                stdout_filter=None,
                stderr_filter=None,
                fail_func=lambda returncode, stderr: returncode != 0):
  if not cwd:
    cwd = os.getcwd()

  child = subprocess.Popen(args,
      stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env)
  stdout, stderr = child.communicate()

  if stdout_filter is not None:
    stdout = stdout_filter(stdout)

  if stderr_filter is not None:
    stderr = stderr_filter(stderr)

  # For Python3 only:
  if isinstance(stdout, bytes) and sys.version_info >= (3, ):
    stdout = stdout.decode('utf-8')

  # For Python3 only:
  if isinstance(stderr, bytes) and sys.version_info >= (3, ):
    stderr = stderr.decode('utf-8')

  if fail_func(child.returncode, stderr):
    raise CalledProcessError(cwd, args, stdout + stderr)

  if print_stdout:
    if isinstance(stdout, str):
      sys.stdout.write(stdout)
    else:
      sys.stdout.write(stdout.decode())
  if print_stderr:
    if isinstance(stderr, str):
      sys.stderr.write(stderr)
    else:
      sys.stderr.write(stderr.decode())

  return stdout


def GetModifiedTime(path):
  # For a symlink, the modified time should be the greater of the link's
  # modified time and the modified time of the target.
  return max(os.lstat(path).st_mtime, os.stat(path).st_mtime)


def IsTimeStale(output, inputs):
  if not os.path.exists(output):
    return True

  output_time = GetModifiedTime(output)
  for i in inputs:
    if GetModifiedTime(i) > output_time:
      return True
  return False


def IsDeviceReady():
  device_state = CheckOutput(['adb', 'get-state'])
  return device_state.strip() == 'device'


def CheckZipPath(name):
  if os.path.normpath(name) != name:
    raise Exception('Non-canonical zip path: %s' % name)
  if os.path.isabs(name):
    raise Exception('Absolute zip path: %s' % name)


def _IsSymlink(zip_file, name):
  zi = zip_file.getinfo(name)

  # The two high-order bytes of ZipInfo.external_attr represent
  # UNIX permissions and file type bits.
  return stat.S_ISLNK(zi.external_attr >> 16)


def ExtractAll(zip_path, path=None, no_clobber=True, pattern=None):
  if path is None:
    path = os.getcwd()
  elif not os.path.exists(path):
    MakeDirectory(path)

  if not zipfile.is_zipfile(zip_path):
    raise Exception('Invalid zip file: %s' % zip_path)

  extracted = []
  with zipfile.ZipFile(zip_path) as z:
    for name in z.namelist():
      if name.endswith('/'):
        continue
      if pattern is not None:
        if not fnmatch.fnmatch(name, pattern):
          continue
      CheckZipPath(name)
      if no_clobber:
        output_path = os.path.join(path, name)
        if os.path.exists(output_path):
          raise Exception(
              'Path already exists from zip: %s %s %s'
              % (zip_path, name, output_path))
      if _IsSymlink(z, name):
        dest = os.path.join(path, name)
        MakeDirectory(os.path.dirname(dest))
        os.symlink(z.read(name), dest)
        extracted.append(dest)
      else:
        z.extract(name, path)
        extracted.append(os.path.join(path, name))

  return extracted


def AddToZipHermetic(zip_file, zip_path, src_path=None, data=None,
                     compress=None):
  """Adds a file to the given ZipFile with a hard-coded modified time.

  Args:
    zip_file: ZipFile instance to add the file to.
    zip_path: Destination path within the zip file.
    src_path: Path of the source file. Mutually exclusive with |data|.
    data: File data as a string.
    compress: Whether to enable compression. Default is taken from ZipFile
        constructor.
  """
  assert (src_path is None) != (data is None), (
      '|src_path| and |data| are mutually exclusive.')
  CheckZipPath(zip_path)
  zipinfo = zipfile.ZipInfo(filename=zip_path, date_time=HERMETIC_TIMESTAMP)
  zipinfo.external_attr = _HERMETIC_FILE_ATTR

  if src_path and os.path.islink(src_path):
    zipinfo.filename = zip_path
    zipinfo.external_attr |= stat.S_IFLNK << 16  # mark as a symlink
    zip_file.writestr(zipinfo, os.readlink(src_path))
    return

  # zipfile.write() does
  #     external_attr = (os.stat(src_path)[0] & 0xFFFF) << 16
  # but we want to use _HERMETIC_FILE_ATTR, so manually set
  # the few attr bits we care about.
  if src_path:
    st = os.stat(src_path)
    for mode in (stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH):
      if st.st_mode & mode:
        zipinfo.external_attr |= mode << 16

  if src_path:
    with open(src_path, 'rb') as f:
      data = f.read()

  # zipfile will deflate even when it makes the file bigger. To avoid
  # growing files, disable compression at an arbitrary cut off point.
  if len(data) < 16:
    compress = False

  # None converts to ZIP_STORED, when passed explicitly rather than the
  # default passed to the ZipFile constructor.
  compress_type = zip_file.compression
  if compress is not None:
    compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
  zip_file.writestr(zipinfo, data, compress_type)


def DoZip(inputs, output, base_dir=None, compress_fn=None,
          zip_prefix_path=None):
  """Creates a zip file from a list of files.

  Args:
    inputs: A list of paths to zip, or a list of (zip_path, fs_path) tuples.
    output: Path, fileobj, or ZipFile instance to add files to.
    base_dir: Prefix to strip from inputs.
    compress_fn: Applied to each input to determine whether or not to compress.
        By default, items will be |zipfile.ZIP_STORED|.
    zip_prefix_path: Path prepended to file path in zip file.
  """
  if base_dir is None:
    base_dir = '.'
  input_tuples = []
  for tup in inputs:
    if isinstance(tup, str):
      tup = (os.path.relpath(tup, base_dir), tup)
    input_tuples.append(tup)

  # Sort by zip path to ensure stable zip ordering.
  input_tuples.sort(key=lambda tup: tup[0])

  out_zip = output
  if not isinstance(output, zipfile.ZipFile):
    out_zip = zipfile.ZipFile(output, 'w')

  try:
    for zip_path, fs_path in input_tuples:
      if zip_prefix_path:
        zip_path = os.path.join(zip_prefix_path, zip_path)
      compress = compress_fn(zip_path) if compress_fn else None
      AddToZipHermetic(out_zip, zip_path, src_path=fs_path, compress=compress)
  finally:
    if output is not out_zip:
      out_zip.close()


def ZipDir(output, base_dir, compress_fn=None, zip_prefix_path=None):
  """Creates a zip file from a directory."""
  inputs = []
  for root, _, files in os.walk(base_dir):
    for f in files:
      inputs.append(os.path.join(root, f))

  with AtomicOutput(output) as f:
    DoZip(inputs, f, base_dir, compress_fn=compress_fn,
          zip_prefix_path=zip_prefix_path)


def MatchesGlob(path, filters):
  """Returns whether the given path matches any of the given glob patterns."""
  return filters and any(fnmatch.fnmatch(path, f) for f in filters)


def MergeZips(output, input_zips, path_transform=None, compress=None):
  """Combines all files from |input_zips| into |output|.

  Args:
    output: Path, fileobj, or ZipFile instance to add files to.
    input_zips: Iterable of paths to zip files to merge.
    path_transform: Called for each entry path. Returns a new path, or None to
        skip the file.
    compress: Overrides compression setting from origin zip entries.
  """
  path_transform = path_transform or (lambda p: p)
  added_names = set()

  out_zip = output
  if not isinstance(output, zipfile.ZipFile):
    out_zip = zipfile.ZipFile(output, 'w')

  try:
    for in_file in input_zips:
      with zipfile.ZipFile(in_file, 'r') as in_zip:
        # ijar creates zips with null CRCs.
        in_zip._expected_crc = None
        for info in in_zip.infolist():
          # Ignore directories.
          if info.filename[-1] == '/':
            continue
          dst_name = path_transform(info.filename)
          if not dst_name:
            continue
          already_added = dst_name in added_names
          if not already_added:
            if compress is not None:
              compress_entry = compress
            else:
              compress_entry = info.compress_type != zipfile.ZIP_STORED
            AddToZipHermetic(
                out_zip,
                dst_name,
                data=in_zip.read(info),
                compress=compress_entry)
            added_names.add(dst_name)
  finally:
    if output is not out_zip:
      out_zip.close()


def PrintWarning(message):
  print('WARNING: ' + message)


def PrintBigWarning(message):
  print('*****     ' * 8)
  PrintWarning(message)
  print('*****     ' * 8)


def GetSortedTransitiveDependencies(top, deps_func):
  """Gets the list of all transitive dependencies in sorted order.

  There should be no cycles in the dependency graph.

  Args:
    top: a list of the top level nodes
    deps_func: A function that takes a node and returns its direct dependencies.
  Returns:
    A list of all transitive dependencies of nodes in top, in order (a node will
    appear in the list at a higher index than all of its dependencies).
  """
  deps_map = collections.OrderedDict()
  def discover(nodes):
    for node in nodes:
      if node in deps_map:
        continue
      deps = deps_func(node)
      discover(deps)
      deps_map[node] = deps

  discover(top)
  return list(deps_map)


def GetPythonDependenciesEx():
  """Gets the paths of imported non-system python modules.

  A path is assumed to be a "system" import if it is outside of chromium's
  src/. The paths will be relative to the current directory.
  """
  _ForceLazyModulesToLoad()
  module_paths = (m.__file__ for m in sys.modules.values()
                  if m is not None and hasattr(m, '__file__'))
  abs_module_paths = list(map(os.path.abspath, filter(lambda p: p is not None, module_paths)))

  assert os.path.isabs(DIR_SOURCE_ROOT)
  non_system_module_paths = [
      p for p in abs_module_paths if p.startswith(DIR_SOURCE_ROOT)]
  def ConvertPycToPy(s):
    if s.endswith('.pyc'):
      return s[:-1]
    return s

  non_system_module_paths = list(map(ConvertPycToPy, non_system_module_paths))
  non_system_module_paths = list(map(os.path.relpath, non_system_module_paths))
  return sorted(set(non_system_module_paths))


def _ComputePythonDependencies():
  """Gets the paths of imported non-system python modules.

  A path is assumed to be a "system" import if it is outside of chromium's
  src/. The paths will be relative to the current directory.
  """
  _ForceLazyModulesToLoad()
  module_paths = (m.__file__ for m in sys.modules.values()
                  if m is not None and hasattr(m, '__file__'))
  abs_module_paths = list(map(os.path.abspath, filter(lambda p: p is not None, module_paths)))

  assert os.path.isabs(DIR_SOURCE_ROOT)
  non_system_module_paths = [
      p for p in abs_module_paths if p.startswith(DIR_SOURCE_ROOT)]
  def ConvertPycToPy(s):
    if s.endswith('.pyc'):
      return s[:-1]
    return s

  non_system_module_paths = list(map(ConvertPycToPy, non_system_module_paths))
  non_system_module_paths = list(map(os.path.relpath, non_system_module_paths))
  return sorted(set(non_system_module_paths))


def GetPythonDependencies():
  """Gets the paths of imported non-system python modules.

  A path is assumed to be a "system" import if it is outside of chromium's
  src/. The paths will be relative to the current directory.
  """
  _ForceLazyModulesToLoad()
  module_paths = (m.__file__ for m in sys.modules.values()
                  if m is not None and hasattr(m, '__file__'))
  abs_module_paths = list(map(os.path.abspath, filter(lambda p: p is not None, module_paths)))

  assert os.path.isabs(DIR_SOURCE_ROOT)
  non_system_module_paths = [
      p for p in abs_module_paths if p.startswith(DIR_SOURCE_ROOT)]
  def ConvertPycToPy(s):
    if s.endswith('.pyc'):
      return s[:-1]
    return s

  non_system_module_paths = list(map(ConvertPycToPy, non_system_module_paths))
  non_system_module_paths = list(map(os.path.relpath, non_system_module_paths))
  return sorted(set(non_system_module_paths))


def _ForceLazyModulesToLoad():
  """Forces any lazily imported modules to fully load themselves.

  Inspecting the modules' __file__ attribute causes lazily imported modules
  (e.g. from email) to get fully imported and update sys.modules. Iterate
  over the values until sys.modules stabilizes so that no modules are missed.
  """
  while True:
    num_modules_before = len(list(sys.modules.keys()))
    for m in list(sys.modules.values()):
      if m is not None and hasattr(m, '__file__'):
        _ = m.__file__
    num_modules_after = len(list(sys.modules.keys()))
    if num_modules_before == num_modules_after:
      break

def AddDepfileOption(parser):
  if hasattr(parser, 'add_option'):
    func = parser.add_option
  else:
    func = parser.add_argument
  func('--depfile',
       help='Path to depfile. This must be specified as the '
            'action\'s first output.')


def WriteDepfileEx(path, dependencies):
  with open(path, 'w') as depfile:
    depfile.write(path)
    depfile.write(': ')
    depfile.write(' '.join(dependencies))
    depfile.write('\n')


def WriteDepfile(depfile_path, first_gn_output, inputs=None, add_pydeps=True):
  assert depfile_path != first_gn_output  # http://crbug.com/646165
  assert not isinstance(inputs, str)  # Easy mistake to make
  inputs = inputs or []
  if add_pydeps:
    inputs = _ComputePythonDependencies() + inputs
  MakeDirectory(os.path.dirname(depfile_path))
  # Ninja does not support multiple outputs in depfiles.
  with open(depfile_path, 'w') as depfile:
    depfile.write(first_gn_output.replace(' ', '\\ '))
    depfile.write(': ')
    depfile.write(' '.join(i.replace(' ', '\\ ') for i in inputs))
    depfile.write('\n')


def ExpandFileArgs(args):
  """Replaces file-arg placeholders in args.

  These placeholders have the form:
    @FileArg(filename:key1:key2:...:keyn)

  The value of such a placeholder is calculated by reading 'filename' as json.
  And then extracting the value at [key1][key2]...[keyn].

  Note: This intentionally does not return the list of files that appear in such
  placeholders. An action that uses file-args *must* know the paths of those
  files prior to the parsing of the arguments (typically by explicitly listing
  them in the action's inputs in build files).
  """
  new_args = list(args)
  file_jsons = dict()
  r = re.compile('@FileArg\((.*?)\)')
  for i, arg in enumerate(args):
    match = r.search(arg)
    if not match:
      continue

    lookup_path = match.group(1).split(':')
    file_path = lookup_path[0]
    if not file_path in file_jsons:
      with open(file_path) as f:
        file_jsons[file_path] = json.load(f)

    expansion = file_jsons[file_path]
    for k in lookup_path[1:]:
      expansion = expansion[k]

    # This should match ParseGnList. The output is either a GN-formatted list
    # or a literal (with no quotes).
    if isinstance(expansion, list):
      new_args[i] = (arg[:match.start()] + gn_helpers.ToGNString(expansion) +
                     arg[match.end():])
    else:
      new_args[i] = arg[:match.start()] + str(expansion) + arg[match.end():]

  return new_args


def ReadSourcesList(sources_list_file_name):
  """Reads a GN-written file containing list of file names and returns a list.

  Note that this function should not be used to parse response files.
  """
  with open(sources_list_file_name) as f:
    return [file_name.strip() for file_name in f]


def CallAndWriteDepfileIfStale(function, options, record_path=None,
                               input_paths=None, input_strings=None,
                               output_paths=None, force=False,
                               pass_changes=False, depfile_deps=None,
                               add_pydeps=True):
  """Wraps md5_check.CallAndRecordIfStale() and writes a depfile if applicable.

  Depfiles are automatically added to output_paths when present in the |options|
  argument. They are then created after |function| is called.

  By default, only python dependencies are added to the depfile. If there are
  other input paths that are not captured by GN deps, then they should be listed
  in depfile_deps. It's important to write paths to the depfile that are already
  captured by GN deps since GN args can cause GN deps to change, and such
  changes are not immediately reflected in depfiles (http://crbug.com/589311).
  """
  if not output_paths:
    raise Exception('At least one output_path must be specified.')
  input_paths = list(input_paths or [])
  input_strings = list(input_strings or [])
  output_paths = list(output_paths or [])

  python_deps = None
  if hasattr(options, 'depfile') and options.depfile:
    python_deps = _ComputePythonDependencies()
    input_paths += python_deps
    output_paths += [options.depfile]

  def on_stale_md5(changes):
    args = (changes,) if pass_changes else ()
    function(*args)
    if python_deps is not None:
      all_depfile_deps = list(python_deps) if add_pydeps else []
      if depfile_deps:
        all_depfile_deps.extend(depfile_deps)
      WriteDepfile(options.depfile, output_paths[0], all_depfile_deps,
                   add_pydeps=False)

  md5_check.CallAndRecordIfStale(
      on_stale_md5,
      record_path=record_path,
      input_paths=input_paths,
      input_strings=input_strings,
      output_paths=output_paths,
      force=force,
      pass_changes=True)
