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
%     .files        the paths it read
%
%   THE RESCALING, which is the one thing that must not be got wrong.
%   The MLA grid is aligned by eye against the preview stream (728 x 544 on
%   this rig) and the sidecar records `pitch_px` and the offsets in THOSE
%   pixels, together with the reference size they were quoted against. A raw
%   capture is the full sensor frame (1456 x 1088), so pitch and both offsets
%   must be multiplied by 1456/728 = 2 before they mean anything here.
%
%   Getting this wrong does not raise. It puts every crop between micro-images
%   instead of on one, and the symptom is simply that nothing ever detects --
%   which is indistinguishable, from the outside, from an optics problem. So
%   the conversion is done here, once, and .mla is always in the coordinates of
%   .image. The unscaled original is kept in .mla.reference for reference.
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
  % one of the two was written by something other than this rig.
  if isfield(info, 'shape')
    want = double(info.shape(:))';
    got  = size(img);
    if numel(want) == numel(got) && ~isequal(want, got)
      error('tv_read_capture:shape', ...
            'sidecar says %s, file holds %s', mat2str(want), mat2str(got));
    end
  end

  cap = struct();
  cap.image  = img;
  cap.height = size(img, 1);
  cap.width  = size(img, 2);
  cap.info   = info;
  cap.files  = struct('image', img_path, 'metadata', json_path);

  cap.cam_id = i_get(info, 'cam_id', '');
  cap.tag    = i_get(info, 'tag', '');
  cap.space  = i_get(info, 'space', '');
  cap.t_iso  = i_get(info, 't_iso', '');
  cap.sensor   = i_get(info, 'sensor_metadata', struct());
  cap.pipeline = i_get(info, 'pipeline', struct());
  cap.camera   = i_get(info, 'camera', struct());

  [cap.mla, cap.has_mla] = i_geometry(cap.pipeline, cap.width, cap.height);
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
