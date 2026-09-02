function [tiles, grid] = tv_micro_images(cap, varargin)
%TV_MICRO_IMAGES  Split a plenoptic frame into one image per lenslet.
%
%   TILES = TV_MICRO_IMAGES(CAP) returns an M-by-N cell array. Each cell holds
%   one micro-image: the square patch of sensor behind one lenslet, side
%   round(pitch * crop_scale) pixels, in the frame's native class. Rows of the
%   cell run down the array (lattice index j), columns run across it (index i),
%   so the cell array is laid out the way the micro-images are laid out on the
%   sensor and CELL2MAT(TILES) reassembles a picture.
%
%   [TILES, GRID] = TV_MICRO_IMAGES(...) also returns
%     .i, .j        lattice index of each column and each row
%     .valid        M-by-N logical: which cells are complete micro-images
%     .centres      M-by-N-by-2, each micro-image's centre in frame pixels
%     .side         the tile side in pixels
%     .derotated    whether the tiles were resampled into the lattice axes
%     .mla          the geometry used
%
%   Options (name/value):
%     'Derotate'  true (default) | false
%                 Resample each tile into the LENSLET's axes rather than the
%                 sensor's, so a rotated array yields upright micro-images.
%                 With rotation below 1e-9 degrees this is a no-op and the
%                 exact integer crop is used, so nothing is resampled and
%                 nothing is blurred.
%     'Scale'     defaults to the capture's crop_scale.
%                 Fraction of the pitch to take. 1.0 is edge to edge, which
%                 for square apertures at zero rotation is exactly the cell.
%     'Indices'   'whole' (default) | 'all'
%                 'whole' keeps only lenslets whose full tile is on the
%                 sensor. A partial tile at the edge is smaller for a reason
%                 that has nothing to do with what it sees, and averaging it
%                 in with the others is how the edge of the array comes to
%                 look permanently bad.
%
%   NAMING, because the two are one permute apart and get confused constantly.
%   What this returns is the set of MICRO-IMAGES: N x M of them, each X x Y
%   pixels, one per lenslet, each a little picture of the aperture. The
%   SUB-APERTURE images are the transpose of that data cube -- X x Y of them,
%   each N x M pixels, each an ordinary pinhole view of the scene from one
%   point in the aperture. Those are what a calibration model sees as cameras.
%   TV_SUB_APERTURES(TILES) performs that permute.
%
%   THE 0-BASED / 1-BASED SEAM. The geometry in CAP.MLA is in the Python
%   convention (pixel centres at 0-based integers). MATLAB subscripts are
%   1-based. The conversion happens on exactly one line below, marked, and
%   nowhere else. Every crop origin here therefore matches the one
%   scripts/read_capture.py computes for the same file, to the pixel -- which
%   is the property that lets corners measured in MATLAB be compared with
%   corners measured on the rig.
%
%   Example:
%     cap   = tv_read_capture('raw_left_000001.npy');
%     tiles = tv_micro_images(cap);
%     montage_ = cell2mat(tiles);            % the array, reassembled
%     imshow(tiles{5, 7}, []);               % one lenslet's view
%
%   See also TV_READ_CAPTURE, TV_SUB_APERTURES.

  opt = i_options(struct('Derotate', true, 'Scale', [], 'Indices', 'whole'), ...
                  varargin);

  if ~isstruct(cap) || ~isfield(cap, 'image')
    error('tv_micro_images:usage', 'first argument must be a tv_read_capture struct');
  end
  if ~cap.has_mla
    error('tv_micro_images:noMLA', ...
          ['this capture has no usable MLA geometry: the mla_grid_overlay ' ...
           'stage was %s. Align the grid in the browser before capturing -- ' ...
           'the pitch cannot be recovered afterwards from the pixels alone.'], ...
          i_why(cap));
  end

  mla = cap.mla;
  if isempty(opt.Scale)
    opt.Scale = mla.crop_scale;
  end

  img  = cap.image;
  side = max(1, round(mla.pitch * opt.Scale));
  t    = mla.rotation_deg * pi / 180;
  ct   = cos(t);  st = sin(t);

  gx = (cap.width  - 1) / 2 + mla.offset_x;      % grid origin, 0-based
  gy = (cap.height - 1) / 2 + mla.offset_y;
  ux = mla.pitch * ct;   uy = mla.pitch * st;    % basis: +i moves right
  vx = -mla.pitch * st;  vy = mla.pitch * ct;    %        +j moves down

  % Generous index bounds, then filter. Cheap, and correct at any rotation.
  reach = hypot(cap.width, cap.height);
  n = floor(reach / max(mla.pitch, 1e-6)) + 2;

  derotate = opt.Derotate && abs(mla.rotation_deg) > 1e-9;
  keep_all = strcmpi(opt.Indices, 'all');

  ii = -n:n;  jj = -n:n;
  cx = gx + ii(:)' * ux;                          % separable: centre(i,j)
  cy = gy + ii(:)' * uy;
  ok = false(numel(jj), numel(ii));
  for r = 1:numel(jj)
    for c = 1:numel(ii)
      ok(r, c) = i_whole(cx(c) + jj(r) * vx, cy(c) + jj(r) * vy, ...
                         side, derotate, ct, st, cap.width, cap.height);
    end
  end

  if keep_all
    rows = 1:numel(jj);  cols = 1:numel(ii);
    % Still trim the all-empty border, or the cell array is mostly nothing.
    inside = false(numel(jj), numel(ii));
    for r = rows
      for c = cols
        x = cx(c) + jj(r) * vx;  y = cy(c) + jj(r) * vy;
        inside(r, c) = x > -side && x < cap.width + side && ...
                       y > -side && y < cap.height + side;
      end
    end
    rows = find(any(inside, 2));  cols = find(any(inside, 1));
    valid = ok(rows, cols);
  else
    rows = find(any(ok, 2));  cols = find(any(ok, 1));
    if isempty(rows)
      error('tv_micro_images:noWholeTiles', ...
            ['no lenslet yields a complete %d px tile in a %dx%d frame. ' ...
             'The pitch (%.2f px here) is almost certainly wrong for this ' ...
             'frame size -- check reference_width in the sidecar.'], ...
            side, cap.width, cap.height, mla.pitch);
    end
    valid = ok(rows, cols);
  end

  ivals = ii(cols);  jvals = jj(rows);
  tiles = cell(numel(rows), numel(cols));
  centres = zeros(numel(rows), numel(cols), 2);
  zero = zeros(side, side, class(img));

  for r = 1:numel(rows)
    for c = 1:numel(cols)
      x = gx + ivals(c) * ux + jvals(r) * vx;
      y = gy + ivals(c) * uy + jvals(r) * vy;
      centres(r, c, 1) = x;  centres(r, c, 2) = y;
      if ~valid(r, c)
        tiles{r, c} = zero;
        continue;
      end
      if derotate
        tiles{r, c} = i_sample(img, x, y, side, ct, st);
      else
        x0 = round(x - side / 2);  y0 = round(y - side / 2);
        % -------- the only 0-based -> 1-based conversion in this file --------
        tiles{r, c} = img(y0 + 1 : y0 + side, x0 + 1 : x0 + side);
        % ---------------------------------------------------------------------
      end
    end
  end

  grid = struct('i', ivals, 'j', jvals, 'valid', valid, 'centres', centres, ...
                'side', side, 'derotated', derotate, 'scale', opt.Scale, ...
                'mla', mla);
end


function tf = i_whole(cx, cy, side, derotate, ct, st, W, H)
%I_WHOLE  Is the sampling window entirely on the sensor?
%   Tests the window the extractor will actually read, not a padded
%   approximation of it: a bound a pixel or two conservative rejects the
%   outermost lenslet most of the time, and which one it picks then changes
%   with a sub-pixel shift of the offset.
  half = side / 2;
  if derotate
    tf = true;
    for sx = [-1 1]
      for sy = [-1 1]
        x = cx + sx * half * ct - sy * half * st;
        y = cy + sx * half * st + sy * half * ct;
        if x < 0 || y < 0 || x > W - 1 || y > H - 1
          tf = false;  return;
        end
      end
    end
  else
    x0 = round(cx - half);  y0 = round(cy - half);
    tf = x0 >= 0 && y0 >= 0 && x0 + side <= W && y0 + side <= H;
  end
end


function tile = i_sample(img, cx, cy, side, ct, st)
%I_SAMPLE  Bilinear resample of one tile along the lattice basis.
%   Sampled once, straight from the full frame, rather than cropping and then
%   rotating: two resampling steps visibly blur a 100 px tile, and blur is
%   precisely what makes a focus judgement wrong.
  o = (0:side-1) - (side - 1) / 2;
  % MESHGRID ORDER. MATLAB's meshgrid(x,y) varies its FIRST output along
  % columns; NumPy's meshgrid(..., indexing='ij') varies its first along rows.
  % Writing this the NumPy way here silently transposes every de-rotated tile
  % -- the sizes match, the self-test's shape checks pass, and the pixels are
  % wrong. Caught by comparing against the Python extractor on the same file
  % (mean |difference| 83.7 of 255), not by any check internal to MATLAB, so
  % the ramp assertion in tv_selftest now pins it.
  [aa, bb] = meshgrid(o, o);            % aa along u (cols), bb along v (rows)
  sx = cx + aa * ct - bb * st;
  sy = cy + aa * st + bb * ct;

  cls = class(img);
  src = double(img);
  % +1 for MATLAB subscripts; interp2's default grid is 1:n.
  vals = interp2(src, sx + 1, sy + 1, 'linear');
  vals(~isfinite(vals)) = 0;            % outside the frame; i_whole excludes it

  if isinteger(img)
    lo = double(intmin(cls));  hi = double(intmax(cls));
    tile = cast(min(max(round(vals), lo), hi), cls);
  else
    tile = cast(vals, cls);
  end
end


function why = i_why(cap)
  if ~isfield(cap.mla, 'reference') || isempty(fieldnames(cap.mla.reference))
    why = 'not in the pipeline at all';
  elseif ~cap.mla.enabled
    why = 'present but disabled';
  else
    why = sprintf('enabled but its pitch (%.3f px) is not usable', cap.mla.pitch);
  end
end


function opt = i_options(opt, args)
%I_OPTIONS  Minimal name/value parsing. Written out rather than using
%   inputParser so these functions run unchanged under Octave.
  if mod(numel(args), 2) ~= 0
    error('tv_micro_images:options', 'options must be name/value pairs');
  end
  known = fieldnames(opt);
  for k = 1:2:numel(args)
    name = args{k};
    hit = find(strcmpi(name, known), 1);
    if isempty(hit)
      error('tv_micro_images:option', 'unknown option "%s". Known: %s', ...
            num2str(name), strjoin(known', ', '));
    end
    opt.(known{hit}) = args{k + 1};
  end
end
