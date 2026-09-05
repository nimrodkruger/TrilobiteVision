function cap = tv_read_capture(path)
%TV_READ_CAPTURE  Load one TrilobiteVision capture: image plus its metadata.
%
%   CAP = TV_READ_CAPTURE(PATH) accepts the .npy file, the .json sidecar, or
%   the stem shared by both, from either camera, in either 'view' or 'raw'
%   mode. It returns a struct:
%
%     .image        the pixels, in their native class (uint8 / uint16)
%     .width        columns
%     .height       rows
%     .cam_id       'left' or 'right'
%     .tag          'raw' or 'view'
%     .space        'raw', 'mono8', 'mono16'
%     .t_iso        wall-clock time of capture
%     .sensor       exposure, gain and whatever else libcamera reported
%     .pipeline     every processing stage's full parameter set
%     .camera       the camera description (model, resolutions)
%     .info         the whole sidecar, unmodified
%     .has_mla      true if an MLA grid stage was present AND enabled
%     .mla          the grid geometry, RESCALED TO THIS FRAME (see below)
%     .orientation  how the frame was turned/mirrored at acquisition:
%                   .rotate_deg (clockwise), .flip_horizontal, .flip_vertical.
%                   Already applied to .image and to .mla -- provenance, not a
%                   correction to undo.
%     .files        the paths it read
%
%     .trimmed_padding  columns of raw row-stride padding removed on load
%
%   TWO THINGS ARE RECONCILED HERE, and both are silent when wrong.
%
%   1. STRIDE PADDING. A raw buffer's rows are padded to a hardware-friendly
%   stride and the array is shaped by that stride, not by the image width. The
%   IMX296 is 1456 px wide; 1456 is not a multiple of 32, so an 8-bit raw frame
%   arrives 1088 x 1472 with sixteen columns on the right that are not image
%   data. Left in, they make the frame 2.022x the preview horizontally against
%   2.000x vertically -- an anisotropic rescale -- and, worse, move the frame
%   CENTRE 8 px right, which shifts every micro-image by a quarter of a
%   checkerboard square. Captures are trimmed at source now; this trims the
%   files already on disk, using camera.full_resolution from the sidecar.
%
%   2. UNITS. The MLA parameters are recorded in FULL-RESOLUTION SENSOR pixels,
%   alongside the reference frame they are expressed in, so for a raw capture
%   the conversion below is the identity. Older captures recorded them in
%   preview pixels against a 728-wide reference; those still read correctly,
%   because the conversion is driven by the recorded reference either way.
%   .mla is always in the coordinates of .image, and .mla.reference keeps the
%   numbers exactly as recorded.
%
%   COORDINATES. .mla is in the Python convention: pixel centres at 0-based
%   integers, x to the right, y DOWN, the frame centre at ((W-1)/2, (H-1)/2).
%   That is deliberate -- it makes every number here directly comparable with
%   the sidecar, with scripts/read_capture.py, and with the corners the rig
%   records. Conversion to MATLAB's 1-based subscripts happens only where an
%   array is actually indexed, inside tv_micro_images.
%
%   Example:
%     cap = tv_read_capture('raw_left_000001_095157_878463.npy');
%     imshow(cap.image, []);  title(sprintf('%s  %s', cap.cam_id, cap.t_iso));
%
%   See also TV_READ_NPY, TV_MICRO_IMAGES, TV_SUB_APERTURES, DEMO_READ_CAPTURE.

  if nargin < 1 || isempty(path)
    error('tv_read_capture:usage', 'tv_read_capture(path)');
  end

  [json_path, img_path] = i_resolve(path);

  txt  = fileread(json_path);
  info = jsondecode(txt);

  if isempty(img_path)
    img_path = fullfile(fileparts(json_path), info.file);
  end
  if exist(img_path, 'file') ~= 2
    error('tv_read_capture:noImage', ...
          ['sidecar %s names image file "%s", which is not beside it. ' ...
           'The pair must travel together: pixels without the metadata are ' ...
           'not data.'], json_path, info.file);
  end

  [~, ~, ext] = fileparts(img_path);
  switch lower(ext)
    case '.npy'
      img = tv_read_npy(img_path);
    case {'.png', '.tif', '.tiff'}
      img = imread(img_path);
    otherwise
      error('tv_read_capture:format', 'unsupported image format "%s"', ext);
  end

  % The sidecar records the shape independently of the file. If they disagree,
  % one of the two was written by something other than this rig. Checked before
  % any trimming, so it compares like with like.
  if isfield(info, 'shape')
    want = double(info.shape(:))';
    got  = size(img);
    if numel(want) == numel(got) && ~isequal(want, got)
      error('tv_read_capture:shape', ...
            'sidecar says %s, file holds %s', mat2str(want), mat2str(got));
    end
  end

  [img, trimmed] = i_trim_stride(img, info);

  cap = struct();
  cap.image  = img;
  cap.height = size(img, 1);
  cap.width  = size(img, 2);
  cap.trimmed_padding = trimmed;
  cap.info   = info;
  cap.files  = struct('image', img_path, 'metadata', json_path);

  cap.cam_id = i_get(info, 'cam_id', '');
  cap.tag    = i_get(info, 'tag', '');
  cap.space  = i_get(info, 'space', '');
  cap.t_iso  = i_get(info, 't_iso', '');
  cap.sensor   = i_get(info, 'sensor_metadata', struct());
  cap.pipeline = i_get(info, 'pipeline', struct());
  cap.camera   = i_get(info, 'camera', struct());

  % How the frame was turned and mirrored at acquisition. Lifted out of the
  % sensor metadata because it is the one thing you cannot recover by looking:
  % a turned landscape sensor and a portrait one give the same shaped array,
  % and the difference decides whether the recorded MLA offsets have had their
  % axes swapped. The rig applies this BEFORE anything else touches the pixels,
  % so .image, .mla and grid.centres are all already in the turned frame and
  % nothing here needs to undo it -- these fields are provenance, not a
  % correction to apply.
  cap.orientation = struct( ...
      'rotate_deg',      double(i_get(cap.sensor, 'rotate_deg', 0)), ...
      'flip_horizontal', logical(i_get(cap.sensor, 'flip_horizontal', false)), ...
      'flip_vertical',   logical(i_get(cap.sensor, 'flip_vertical', false)));

  [cap.mla, cap.has_mla] = i_geometry(cap.pipeline, cap.width, cap.height);
end


function [img, trimmed] = i_trim_stride(img, info)
%I_TRIM_STRIDE  Turn a raw buffer into an image: pixel size, then padding.
%
%   A raw buffer's rows are padded to a hardware-friendly stride (64 bytes on
%   this pipeline) and the array is shaped by that stride IN BYTES, stored as
%   uint8 whatever the real pixel size is:
%
%       8-bit  (R8)   1456 px = 1456 bytes -> stride 1472 -> array 1472 wide
%       10-bit (R10)  1456 px = 2912 bytes -> stride 2944 -> array 2944 wide
%
%   The second case is 1472 *uint16* pixels laid out as 2944 bytes, not 1456
%   pixels plus padding. Cropping its width to 1456 keeps the first 728 pixels
%   and half of the next -- structure at the wrong scale, and the reason this
%   reader reported a 2.022 : 1.000 aspect ratio.
%
%   Left undone, the padding makes the MLA rescale anisotropic and moves the
%   frame CENTRE, which the whole grid hangs off, by half the padding.
%
%   Captures are trimmed at source now. This is for files already on disk, and
%   it is safe because the sidecar records the true sensor size.
  trimmed = 0;
  if ~isstruct(info) || ~isfield(info, 'camera') || ...
     ~isfield(info.camera, 'full_resolution')
    return;
  end
  full = double(info.camera.full_resolution(:))';
  if numel(full) ~= 2
    return;
  end
  full_w = full(1);  full_h = full(2);
  [h, w] = size(img);

  if w == full_w || h ~= full_h
    return;                       % already right, or not a stride story
  end

  bytes_per_element = i_element_bytes(class(img));
  row_bytes = w * bytes_per_element;

  for bpp = [1 2]
    want = full_w * bpp;
    if row_bytes < want || row_bytes > want + 256
      continue;
    end
    out = img;
    if bpp == 2 && bytes_per_element == 1
      if mod(w, 2) ~= 0
        break;
      end
      % A row of 2N bytes IS N little-endian uint16 pixels. typecast works on
      % a vector, so the rows are laid end to end first -- img.' because
      % MATLAB is column-major and the bytes run along rows.
      u16 = typecast(reshape(img.', [], 1), 'uint16');
      out = reshape(u16, w / 2, h).';
    end
    if size(out, 2) < full_w
      break;
    end
    trimmed = size(out, 2) - full_w;
    img = out(:, 1:full_w);
    return;
  end

  warning('tv_read_capture:shape', ...
          ['raw buffer is %dx%d of %s (%d bytes per row) and the sensor is ' ...
           '%dx%d -- not a whole number of bytes per pixel plus a stride pad. ' ...
           'Leaving it untouched; most likely a PACKED format.'], ...
          w, h, class(img), row_bytes, full_w, full_h);
end


function n = i_element_bytes(cls)
  switch cls
    case {'uint8', 'int8'},   n = 1;
    case {'uint16', 'int16'}, n = 2;
    case {'uint32', 'int32', 'single'}, n = 4;
    case {'uint64', 'int64', 'double'}, n = 8;
    otherwise, n = 1;
  end
end


function [json_path, img_path] = i_resolve(path)
%I_RESOLVE  Accept the .npy, the .json, or the stem, and find the other one.
  [d, base, ext] = fileparts(path);
  switch lower(ext)
    case '.json'
      json_path = path;  img_path = '';
    case {'.npy', '.png', '.tif', '.tiff'}
      json_path = fullfile(d, [base '.json']);  img_path = path;
    otherwise
      json_path = fullfile(d, [base '.json']);  img_path = '';
      if exist(json_path, 'file') ~= 2
        json_path = [path '.json'];
      end
  end
  if exist(json_path, 'file') ~= 2
    error('tv_read_capture:noSidecar', ...
          ['no JSON sidecar for %s. Every capture is written as a pair; a ' ...
           'lone .npy has no dtype context, no sensor settings and no MLA ' ...
           'geometry, so it cannot be parsed into micro-images.'], path);
  end
end


function v = i_get(s, name, default)
  if isstruct(s) && isfield(s, name)
    v = s.(name);
  else
    v = default;
  end
end


function [mla, ok] = i_geometry(pipeline, width, height)
%I_GEOMETRY  Find the MLA stage in the recorded pipeline and rescale it.
  mla = struct('enabled', false, 'pitch', NaN, 'rotation_deg', 0, ...
               'offset_x', 0, 'offset_y', 0, 'crop_scale', 1, ...
               'width', width, 'height', height, 'scale_applied', 1, ...
               'reference', struct());
  ok = false;
  if ~isstruct(pipeline)
    return;
  end

  % Found by stage TYPE, not by the name it was given in the config -- the
  % name is the operator's, the type is the contract.
  names = fieldnames(pipeline);
  st = [];
  for k = 1:numel(names)
    s = pipeline.(names{k});
    if isstruct(s) && isfield(s, 'type') && strcmp(s.type, 'mla_grid_overlay')
      st = s;
      break;
    end
  end
  if isempty(st)
    return;
  end

  mla.reference = st;
  mla.enabled   = isfield(st, 'enabled') && logical(st.enabled);

  ref_w = i_get(st, 'reference_width',  width);
  ref_h = i_get(st, 'reference_height', height);
  if ~isfinite(ref_w) || ref_w <= 0, ref_w = width;  end
  if ~isfinite(ref_h) || ref_h <= 0, ref_h = height; end

  sx = width / ref_w;
  sy = height / ref_h;
  if abs(sx - sy) > 1e-6 * max(sx, sy)
    error('tv_read_capture:anisotropic', ...
          ['the grid was aligned on a %dx%d frame and this capture is ' ...
           '%dx%d (x%.4f horizontally, x%.4f vertically). Pitch has no ' ...
           'single value under an anisotropic rescale.'], ...
          ref_w, ref_h, width, height, sx, sy);
  end

  mla.pitch        = double(i_get(st, 'pitch_px', NaN)) * sx;
  mla.rotation_deg = double(i_get(st, 'rotation_deg', 0));
  mla.offset_x     = double(i_get(st, 'offset_x', 0)) * sx;
  mla.offset_y     = double(i_get(st, 'offset_y', 0)) * sy;
  mla.crop_scale   = double(i_get(st, 'crop_scale', 1));
  mla.scale_applied = sx;

  ok = mla.enabled && isfinite(mla.pitch) && mla.pitch > 1;
end
