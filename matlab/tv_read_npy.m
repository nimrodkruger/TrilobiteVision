function [a, info] = tv_read_npy(path)
%TV_READ_NPY  Read a NumPy .npy file into a MATLAB array.
%
%   A = TV_READ_NPY(PATH) returns the array stored in the .npy file at PATH,
%   in its native class (uint8, uint16, single, double, ...).
%
%   [A, INFO] = TV_READ_NPY(PATH) also returns a struct describing the file:
%   .descr (the NumPy dtype string), .fortran_order, .shape, .class,
%   .byte_order and .header_version.
%
%   Why this exists rather than a toolbox call: .npy is the project's capture
%   format because it is lossless, holds the sensor's native dtype with no
%   conversion, and needs no image library to write on the Pi. MATLAB has no
%   reader for it, and the ones on File Exchange vary in whether they handle
%   the v2 header, big-endian dtypes, or C versus Fortran ordering. All three
%   appear in real data, so they are all handled here.
%
%   THE ORDERING TRAP. NumPy writes C order by default: the LAST axis varies
%   fastest. MATLAB's reshape fills the FIRST dimension fastest. Reading the
%   bytes straight into reshape(v, shape) therefore returns the transpose of
%   the image, silently, and every downstream coordinate is then wrong in a way
%   that looks like a calibration error rather than a bug. The fix is to
%   reshape into the reversed shape and permute back, which is what happens
%   below.
%
%   See also TV_READ_CAPTURE.

  if nargin < 1 || isempty(path)
    error('tv_read_npy:usage', 'tv_read_npy(path)');
  end
  if exist(path, 'file') ~= 2
    error('tv_read_npy:notFound', 'no such file: %s', path);
  end

  fid = fopen(path, 'r');
  if fid < 0
    error('tv_read_npy:open', 'cannot open %s', path);
  end
  closer = onCleanup(@() fclose(fid));

  magic = fread(fid, 6, '*uint8')';
  if ~isequal(magic, uint8([147 78 85 77 80 89]))     % \x93 N U M P Y
    error('tv_read_npy:magic', ...
          '%s is not a .npy file (bad magic number)', path);
  end

  ver = fread(fid, 2, '*uint8')';
  major = double(ver(1));
  if major == 1
    hlen = double(fread(fid, 1, 'uint16', 0, 'ieee-le'));
  elseif major == 2 || major == 3
    % v2 widened the header length field to 4 bytes; v3 only changed the
    % header's text encoding to UTF-8, which ASCII parsing handles unchanged.
    hlen = double(fread(fid, 1, 'uint32', 0, 'ieee-le'));
  else
    error('tv_read_npy:version', 'unsupported .npy version %d.%d', ...
          major, double(ver(2)));
  end

  header = fread(fid, hlen, '*char')';

  descr = i_match(header, '''descr''\s*:\s*''([^'']+)''');
  if isempty(descr)
    error('tv_read_npy:header', 'no dtype in header: %s', header);
  end
  fortran = ~isempty(regexp(header, '''fortran_order''\s*:\s*True', 'once'));

  shape_txt = i_match(header, '''shape''\s*:\s*\(([^)]*)\)');
  shape = sscanf(strrep(shape_txt, ',', ' '), '%f')';
  if isempty(shape)
    shape = 1;                       % a 0-d array holds one element
  end

  [cls, order, precision] = i_dtype(descr);

  n = prod(shape);
  raw = fread(fid, n, precision, 0, order);
  if numel(raw) ~= n
    error('tv_read_npy:truncated', ...
          '%s: expected %d elements, read %d', path, n, numel(raw));
  end

  dims = shape;
  if numel(dims) == 1
    dims = [dims 1];
  end
  if fortran
    a = reshape(raw, dims);
  else
    a = reshape(raw, fliplr(dims));
    a = permute(a, ndims(a):-1:1);
  end

  if nargout > 1
    info = struct('descr', descr, 'fortran_order', fortran, ...
                  'shape', shape, 'class', cls, 'byte_order', order, ...
                  'header_version', major);
  end
end


function s = i_match(text, pattern)
  tok = regexp(text, pattern, 'tokens', 'once');
  if isempty(tok)
    s = '';
  else
    s = tok{1};
  end
end


function [cls, order, precision] = i_dtype(descr)
%I_DTYPE  NumPy dtype string -> MATLAB class, fread byte order and precision.
  switch descr(1)
    case '<',  order = 'ieee-le';  body = descr(2:end);
    case '>',  order = 'ieee-be';  body = descr(2:end);
    case {'|', '='}, order = 'ieee-le';  body = descr(2:end);
    otherwise, order = 'ieee-le';  body = descr;
  end

  map = { 'u1', 'uint8';   'i1', 'int8'; ...
          'u2', 'uint16';  'i2', 'int16'; ...
          'u4', 'uint32';  'i4', 'int32'; ...
          'u8', 'uint64';  'i8', 'int64'; ...
          'f4', 'single';  'f8', 'double';  'b1', 'uint8' };

  hit = strcmp(body, map(:, 1));
  if ~any(hit)
    error('tv_read_npy:dtype', ...
          ['unsupported dtype ''%s''. Captures are written as the sensor''s ' ...
           'native integer type, so this usually means the file is not a ' ...
           'TrilobiteVision capture.'], descr);
  end
  cls = map{hit, 2};
  precision = ['*' cls];             % '*' keeps the class rather than double
end
