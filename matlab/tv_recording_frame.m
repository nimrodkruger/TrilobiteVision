function [img, meta] = tv_recording_frame(rec, cam, k)
%TV_RECORDING_FRAME  One frame out of a recording, oriented for viewing.
%
%   [IMG, META] = TV_RECORDING_FRAME(REC, CAM, K) returns the K-th STORED
%   frame of head CAM from a recording opened by TV_READ_RECORDING, in its
%   native class, with the camera's rotation and mirrors applied.
%
%   K counts stored frames, from 1. **It is not a time index.** A recording
%   may have dropped frames, so the exposure a frame belongs to is
%   META.seq, and the only clock with a defined relation to exposure is
%   META.sensor_timestamp. To step through a recording at the right spacing,
%   walk META.seq, not K.
%
%   META, from the chunk index:
%     .seq                sequence number AS EXPOSED
%     .sensor_timestamp   the driver's exposure clock, nanoseconds, in this
%                         head's own domain. [] when the driver did not supply
%                         one -- never substituted, because a wall-clock value
%                         here would produce an index that looks usable for
%                         timing and is not.
%     .validity           'science', 'diagnostic' or 'unvalidated' for this
%                         frame alone. Admission runs per frame, so a
%                         recording can begin as science and stop being so.
%     .reservations       why, when it is not science
%     .chunk, .row        where it came from, for going back to the file
%
%   Reads only the bytes of the one frame: it seeks past the .npy header and
%   past the preceding rows rather than loading the chunk, so stepping through
%   a multi-gigabyte recording costs one frame of memory.
%
%   Example -- every frame of the left head, in exposure order:
%
%     rec = tv_read_recording(dir);
%     for k = 1:rec.heads.left.stored
%       [img, m] = tv_recording_frame(rec, 'left', k);
%       fprintf('seq %d  mean %.1f\n', m.seq, mean(img(:)));
%     end
%
%   See also TV_READ_RECORDING, TV_READ_NPY.

  if nargin < 3
    error('tv_recording_frame:usage', 'tv_recording_frame(rec, cam, k)');
  end
  cam = char(cam);
  if ~isfield(rec.heads, cam)
    error('tv_recording_frame:noHead', ...
          'no head %s in this recording; have: %s', cam, strjoin(rec.cams, ', '));
  end
  head = rec.heads.(cam);
  k = double(k);
  if k < 1 || k > head.stored || k ~= fix(k)
    error('tv_recording_frame:range', ...
          'k must be an integer in 1..%d (stored frames for %s)', ...
          head.stored, cam);
  end

  % -- locate the frame -------------------------------------------------
  %
  % By walking the chunk indexes, not by arithmetic on a nominal chunk size:
  % the last chunk is short, and a chunk interrupted by a power cut is shorter
  % than its own header claims.
  remaining = k;
  chunk = [];
  row = 0;
  for i = 1:numel(head.chunks)
    c = head.chunks(i);
    if remaining <= c.n
      chunk = c;
      row = remaining;
      break;
    end
    remaining = remaining - c.n;
  end
  if isempty(chunk)
    error('tv_recording_frame:internal', ...
          'frame %d not found although %s claims %d stored', k, cam, head.stored);
  end

  % -- read it ----------------------------------------------------------
  [img, descr, shape] = i_read_frame(chunk, row);

  % The index is authoritative about how many frames a chunk holds; the .npy
  % header is authoritative about where they are. They disagree only when a
  % write was interrupted, and then the header is the one that is stale --
  % it was written for the full chunk and corrected at close, which never
  % happened. Say so rather than returning a row of zeros as data.
  if shape(1) < chunk.n
    warning('tv_recording_frame:short', ...
            ['%s declares %d frames in its header and its index names %d. ' ...
             'The write was interrupted; frames past %d are not there.'], ...
            chunk.npy, shape(1), chunk.n, shape(1));
  end

  meta = i_meta(chunk, row);
  meta.chunk = chunk.npy;
  meta.row = row;
  meta.dtype = descr;

  % -- orient -----------------------------------------------------------
  %
  % Stored pixels are in the sensor frame: a quarter turn is a full-array copy
  % and at two heads by 30 fps that is memory bandwidth the recording needs for
  % the disk. So the turn happens here, from the transform the journal records.
  %
  % Order matters, and only for the two combinations that include BOTH a
  % quarter turn and a mirror -- which is exactly the error that survives a
  % casual look. Rotate first, then mirror, so "flip horizontal" means "flip
  % what I am looking at" and not "flip the sensor". Same order as the rig
  % applies at acquisition and as scripts/read_capture.py applies on the
  % desktop; all three have to agree or the same recording reads differently
  % in each.
  o = rec.orientation.(cam);
  if isstruct(o) && ~isempty(fieldnames(o))
    turns = mod(fix(double(i_get(o, 'rotate_deg', 0)) / 90), 4);
    if turns ~= 0
      % rot90 with positive k is counter-clockwise; rotate_deg is clockwise
      % as seen in the image, so the sign flips.
      img = rot90(img, -turns);
    end
    if logical(i_get(o, 'flip_horizontal', false))
      img = fliplr(img);
    end
    if logical(i_get(o, 'flip_vertical', false))
      img = flipud(img);
    end
  end
end


% ----------------------------------------------------------------------
function [img, descr, shape] = i_read_frame(chunk, row)
%I_READ_FRAME  Read one (H,W) frame out of a 3-D C-order .npy by seeking.

  % 'l': the .npy header's own length field is little-endian by specification,
  % whatever the array's dtype says. The array bytes are handled explicitly
  % below rather than through fread's precision, which has no byte-order form.
  fid = fopen(chunk.npy, 'r', 'l');
  if fid < 0
    error('tv_recording_frame:open', 'cannot open %s', chunk.npy);
  end
  closer = onCleanup(@() fclose(fid));                         %#ok<NASGU>

  magic = fread(fid, 6, '*uint8')';
  if ~isequal(magic, uint8([147 78 85 77 80 89]))   % \x93NUMPY
    error('tv_recording_frame:notNpy', '%s is not a .npy file', chunk.npy);
  end
  ver = fread(fid, 2, '*uint8');
  if ver(1) == 1
    hlen = double(fread(fid, 1, 'uint16=>double'));
    preamble = 10;
  else
    hlen = double(fread(fid, 1, 'uint32=>double'));
    preamble = 12;
  end
  header = fread(fid, hlen, '*char')';
  data_start = preamble + hlen;

  descr = i_match(header, '''descr''\s*:\s*''([^'']+)''');
  shape_txt = i_match(header, '''shape''\s*:\s*\(([^)]*)\)');
  fortran = ~isempty(regexp(header, '''fortran_order''\s*:\s*True', 'once'));
  if fortran
    % The writer never produces this, and silently reading it as C order
    % would transpose every frame.
    error('tv_recording_frame:fortran', ...
          '%s is Fortran-ordered; this reader expects C order', chunk.npy);
  end

  shape = str2double(regexp(shape_txt, '[^,\s]+', 'match'));
  shape = shape(~isnan(shape));
  if numel(shape) ~= 3
    error('tv_recording_frame:rank', ...
          ['%s has shape (%s); a recording chunk is 3-D (frames, rows, ' ...
           'cols). Use tv_read_capture for a single still.'], ...
          chunk.npy, shape_txt);
  end
  H = shape(2);  W = shape(3);

  [cls, big_endian, bytes_each] = i_class(descr);
  frame_bytes = H * W * bytes_each;
  if chunk.frame_bytes > 0 && chunk.frame_bytes ~= frame_bytes
    error('tv_recording_frame:frameBytes', ...
          ['%s: the index says a frame is %d bytes and the header implies ' ...
           '%d. One of them does not describe this file; refusing rather ' ...
           'than reading at the wrong stride.'], ...
          chunk.npy, chunk.frame_bytes, frame_bytes);
  end

  if fseek(fid, data_start + (row - 1) * frame_bytes, 'bof') ~= 0
    error('tv_recording_frame:seek', ...
          'cannot seek to frame %d of %s (file is short)', row, chunk.npy);
  end
  % Read raw bytes and typecast, rather than letting fread convert. fread's
  % precision string has no byte-order form, so a big-endian dtype would be
  % read with the host's order and come back byte-swapped -- 1024 as 4, which
  % is wrong in a way that looks like a dark frame rather than an error.
  raw = fread(fid, frame_bytes, '*uint8');
  if numel(raw) < frame_bytes
    error('tv_recording_frame:short', ...
          ['frame %d of %s is truncated: %d of %d bytes. The chunk was ' ...
           'cut off mid-frame.'], row, chunk.npy, numel(raw), frame_bytes);
  end
  v = typecast(raw, cls);
  if big_endian && bytes_each > 1
    v = swapbytes(v);
  end

  % C order means the LAST axis varies fastest, so the linear run is one row
  % at a time. MATLAB's reshape fills the FIRST dimension fastest, so reshape
  % to [W H] and transpose -- reshape([H W]) would return the transpose,
  % silently, and put every coordinate at (y,x). Same trap as tv_read_npy.
  img = reshape(v, [W H]).';
end


function m = i_meta(chunk, row)
%I_META  The index record for one row, normalised.
  raw = struct();
  if isstruct(chunk.frames) && numel(chunk.frames) >= row
    raw = chunk.frames(row);
  elseif iscell(chunk.frames) && numel(chunk.frames) >= row
    raw = chunk.frames{row};
  end
  stamp = i_get(raw, 'sensor_timestamp', []);
  if ischar(stamp) || isstring(stamp)
    stamp = [];
  end
  m = struct( ...
    'seq',              double(i_get(raw, 'seq', NaN)), ...
    'sensor_timestamp', stamp, ...
    't_wall',           double(i_get(raw, 't_wall', NaN)), ...
    'validity',         char(i_get(raw, 'validity', 'unknown')), ...
    'reservations',     {i_cellstr(i_get(raw, 'reservations', {}))}, ...
    'observed_max',     i_get(raw, 'observed_max', []));
end


function [cls, big_endian, nbytes] = i_class(descr)
%I_CLASS  NumPy dtype string to a MATLAB class, byte order and sample width.
  d = char(descr);
  order = d(1);
  code = d(2:end);
  if ~any(order == '<>|=')
    % No byte-order prefix, e.g. 'u2'. Treat the whole string as the code.
    order = '|';
    code = d;
  end
  map = {'u1', 'uint8', 1; 'u2', 'uint16', 2; 'u4', 'uint32', 4; ...
         'i1', 'int8', 1;  'i2', 'int16', 2;  'i4', 'int32', 4; ...
         'f4', 'single', 4; 'f8', 'double', 8};
  hit = find(strcmp(map(:, 1), code), 1);
  if isempty(hit)
    error('tv_recording_frame:dtype', 'unsupported dtype %s', d);
  end
  cls = map{hit, 2};
  nbytes = map{hit, 3};
  % Nothing in this project writes big-endian, but a recording copied from a
  % different machine could, and silently byte-swapped sensor counts read as
  % a plausible dark frame rather than as an error.
  big_endian = (order == '>');
end


function s = i_match(header, pattern)
  tok = regexp(header, pattern, 'tokens', 'once');
  if isempty(tok)
    error('tv_recording_frame:header', 'cannot parse the .npy header');
  end
  s = tok{1};
end


function c = i_cellstr(v)
  if isempty(v)
    c = {};
  elseif ischar(v)
    c = {v};
  elseif iscell(v)
    c = cellfun(@char, v, 'UniformOutput', false);
  else
    c = cellstr(string(v));
  end
end


function v = i_get(s, name, default)
  if isstruct(s) && isfield(s, name)
    v = s.(name);
    if isempty(v) && ~islogical(v)
      v = default;
    end
  else
    v = default;
  end
end
