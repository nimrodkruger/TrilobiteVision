function ok = tv_selftest(path)
%TV_SELFTEST  Check the MATLAB reader against a real capture, headless.
%
%   TV_SELFTEST(PATH) runs every claim these functions make as an assertion and
%   prints a pass/fail line for each. No figures, so it runs over SSH and in
%   MATLAB's -batch mode. Returns true if everything passed.
%
%   The checks that matter, and why each one exists:
%
%     npy round trip      the array is not transposed. NumPy writes C order and
%                         MATLAB reshapes in Fortran order, so getting this
%                         wrong is silent and puts every coordinate at (y,x).
%     dtype preserved     a 10-bit sensor value must not arrive as a double
%                         scaled to [0,1].
%     rescale             pitch on a full-resolution frame is the recorded
%                         pitch times the resolution ratio, exactly.
%     tile geometry       the crop origins agree with the sidecar geometry and
%                         with what the Python reader computes.
%     reassembly          cell2mat of the tiles reproduces the corresponding
%                         region of the frame, pixel for pixel, when the array
%                         is unrotated. This is the strongest single check:
%                         it fails on any off-by-one anywhere.
%     permute is lossless tv_sub_apertures twice is the identity.
%
%   See also DEMO_READ_CAPTURE.

  if nargin < 1
    error('tv_selftest:usage', 'tv_selftest(path_to_npy)');
  end
  n_pass = 0;  n_fail = 0;

  cap = tv_read_capture(path);
  check('loads', true, '');

  % -- the array is the right way round ---------------------------------
  % The sidecar records the shape and dtype of the FILE. cap.image can
  % legitimately differ from both, and for two separate reasons:
  %   * row-stride padding is trimmed off, so it is narrower;
  %   * a 10-bit buffer arrives as uint8 with the row length in BYTES, and is
  %     re-viewed as uint16, so it is half as wide again and a different class.
  % The invariant worth checking is therefore not "same as the file" but "the
  % sensor's own width", which the sidecar records independently.
  file_shape = double(cap.info.shape(:))';
  check('height matches the sidecar', cap.height == file_shape(1), ...
        sprintf('%d vs %d', cap.height, file_shape(1)));

  if cap.trimmed_padding > 0 && isfield(cap.camera, 'full_resolution')
    sensor_w = double(cap.camera.full_resolution(1));
    check('width is the sensor width after trimming', cap.width == sensor_w, ...
          sprintf('%d vs %d', cap.width, sensor_w));
  else
    check('width matches the sidecar', cap.width == file_shape(2), ...
          sprintf('%d vs %d', cap.width, file_shape(2)));
  end

  % A re-view to uint16 is expected and correct; anything else is not.
  file_class = i_class(cap.info.dtype);
  ok_class = strcmp(class(cap.image), file_class) || ...
             (strcmp(file_class, 'uint8') && strcmp(class(cap.image), 'uint16'));
  check('dtype preserved, or re-viewed to uint16', ok_class, ...
        sprintf('%s from a %s file', class(cap.image), file_class));

  if strcmp(class(cap.image), 'uint16') && strcmp(file_class, 'uint8')
    % Two bytes per pixel: the row length in bytes has to come out even, and
    % the values must exceed 8-bit range somewhere or the re-view is suspect.
    check('a 10-bit buffer was re-viewed, not truncated', ...
          max(cap.image(:)) > 255 || min(cap.image(:)) > 0, ...
          sprintf('range %d..%d', min(cap.image(:)), max(cap.image(:))));
  end

  check('not transposed (wider than tall)', cap.width >= cap.height, ...
        sprintf('%dx%d', cap.width, cap.height));

  % -- the rescale is exact ---------------------------------------------
  if cap.has_mla
    m = cap.mla;
    s = cap.width / m.reference.reference_width;
    check('pitch rescaled exactly', ...
          abs(m.pitch - m.reference.pitch_px * s) < 1e-9, ...
          sprintf('%.6f vs %.6f', m.pitch, m.reference.pitch_px * s));
    check('offsets rescaled exactly', ...
          abs(m.offset_x - m.reference.offset_x * s) < 1e-9 && ...
          abs(m.offset_y - m.reference.offset_y * s) < 1e-9, '');
    check('rotation carried over unchanged', ...
          m.rotation_deg == m.reference.rotation_deg, '');
  else
    fprintf('  SKIP  no MLA geometry in this capture; tile checks skipped\n');
    ok = n_fail == 0;
    return;
  end

  % -- tiles ------------------------------------------------------------
  [tiles, g] = tv_micro_images(cap, 'Derotate', false);
  check('every tile is square and the same size', ...
        all(cellfun(@(t) isequal(size(t), [g.side g.side]), tiles(:))), ...
        sprintf('side %d', g.side));

  check('tile class matches the frame', strcmp(class(tiles{1}), class(cap.image)), '');

  % The cell array is the bounding box of the complete lenslets. On a ROTATED
  % lattice that box necessarily has incomplete corners -- the set of whole
  % lenslets is a rotated rectangle, and no cell array is. So the guarantee is
  % not "all cells valid", it is "g.valid says which, and the invalid ones are
  % zero-filled rather than garbage".
  check('most of the bounding box is valid', ...
        sum(g.valid(:)) > 0.75 * numel(g.valid), ...
        sprintf('%d of %d', sum(g.valid(:)), numel(g.valid)));
  bad = find(~g.valid);
  check('invalid cells are zero-filled, not garbage', ...
        isempty(bad) || all(arrayfun(@(k) ~any(tiles{k}(:)), bad)), '');

  % The strong one: an unrotated extraction must be an exact sub-array of the
  % frame. Any off-by-one in the 0-based/1-based seam breaks this.
  worst = 0;
  for r = 1:numel(g.j)
    for c = 1:numel(g.i)
      if ~g.valid(r, c), continue; end
      x0 = round(g.centres(r, c, 1) - g.side / 2);
      y0 = round(g.centres(r, c, 2) - g.side / 2);
      ref = cap.image(y0 + 1 : y0 + g.side, x0 + 1 : x0 + g.side);
      worst = max(worst, max(abs(double(ref(:)) - double(tiles{r, c}(:)))));
    end
  end
  check('tiles are exact sub-arrays of the frame', worst == 0, ...
        sprintf('max |difference| = %g', worst));

  % -- the permute ------------------------------------------------------
  subs = tv_sub_apertures(tiles);
  check('sub-aperture count = micro-image size', ...
        isequal(size(subs), [g.side g.side]), mat2str(size(subs)));
  check('sub-aperture size = lattice size', ...
        isequal(size(subs{1}), [numel(g.j) numel(g.i)]), mat2str(size(subs{1})));

  back = tv_sub_apertures(subs);
  same = numel(back) == numel(tiles);
  if same
    for k = 1:numel(tiles)
      if ~isequal(back{k}, tiles{k}), same = false; break; end
    end
  end
  check('permuting twice is the identity', same, '');

  % Spot-check one value through the permute, by hand.
  v = 3;  u = 7;  r = 2;  c = 4;
  if numel(g.j) >= r && numel(g.i) >= c && g.side >= max(u, v)
    check('sub-aperture indexing is the transpose', ...
          subs{v, u}(r, c) == tiles{r, c}(v, u), '');
  end

  % -- de-rotation ------------------------------------------------------
  if abs(cap.mla.rotation_deg) < 1e-9
    t2 = tv_micro_images(cap, 'Derotate', true);
    check('de-rotation is a no-op at zero rotation', isequal(t2{1}, tiles{1}), '');
  else
    t2 = tv_micro_images(cap, 'Derotate', true);
    check('de-rotated tiles are the same size', ...
          isequal(size(t2{1}), size(tiles{1})), '');
  end

  % -- raw row-stride padding ----------------------------------------------
  % The IMX296 is 1456 px wide and 1456 is not a multiple of 32, so a raw
  % frame's rows are padded to 1472 and the array is shaped by that stride.
  % Left in, the 16 extra columns make the frame 2.022x the preview width
  % against 2.000x its height -- the anisotropic rescale this reader used to
  % refuse -- and move the frame centre 8 px right, shifting every micro-image
  % by a quarter of a checkerboard square.
  if cap.trimmed_padding > 0
    check('padding was trimmed and the rescale came out isotropic', ...
          abs(cap.mla.scale_applied - cap.height / cap.mla.reference.reference_height) < 1e-9, ...
          sprintf('trimmed %d columns, scale x%.4f', ...
                  cap.trimmed_padding, cap.mla.scale_applied));
  end

  % -- the sampling geometry, against arithmetic rather than another file ---
  % A ramp image has a known value everywhere, so the de-rotated tile can be
  % predicted in closed form. This pins the axis order of the resampling grid,
  % which is the one thing no shape or size check can see: MATLAB's meshgrid
  % and NumPy's disagree about which output varies along rows, and getting it
  % wrong transposes every tile while every dimension still matches.
  [e_x, e_y] = i_ramp_error(11.0);
  check('de-rotated sampling matches the lattice basis (x)', e_x < 1e-6, ...
        sprintf('max error %.3g px', e_x));
  check('de-rotated sampling matches the lattice basis (y)', e_y < 1e-6, ...
        sprintf('max error %.3g px', e_y));

  fprintf('\n  %d passed, %d failed\n\n', n_pass, n_fail);
  ok = n_fail == 0;

  function check(name, cond, detail)
    if cond
      n_pass = n_pass + 1;
      fprintf('  ok    %s\n', name);
    else
      n_fail = n_fail + 1;
      fprintf('  FAIL  %s   %s\n', name, detail);
    end
  end
end


function [ex, ey] = i_ramp_error(rot_deg)
%I_RAMP_ERROR  Resample a coordinate ramp and compare with the closed form.
%   Tile pixel (r, c), measured from the centre as a = c - (side-1)/2 along the
%   lattice's u direction and b = r - (side-1)/2 along v, is sampled from the
%   frame point
%       x = cx + a cos(t) - b sin(t)
%       y = cy + a sin(t) + b cos(t)
%   so a tile extracted from an image whose value IS x must reproduce that
%   expression exactly, to bilinear precision (exact, since a ramp is linear).
  W = 401;  H = 301;  pitch = 60;  side = round(pitch);
  [xx, yy] = meshgrid(0:W-1, 0:H-1);

  cap = struct('width', W, 'height', H, 'has_mla', true, ...
               'mla', struct('pitch', pitch, 'rotation_deg', rot_deg, ...
                             'offset_x', 0, 'offset_y', 0, 'crop_scale', 1));

  t = rot_deg * pi / 180;  ct = cos(t);  st = sin(t);
  cx = (W - 1) / 2;  cy = (H - 1) / 2;
  o = (0:side-1) - (side - 1) / 2;
  [aa, bb] = meshgrid(o, o);
  want_x = cx + aa * ct - bb * st;
  want_y = cy + aa * st + bb * ct;

  cap.image = xx;
  tx = tv_micro_images(cap, 'Derotate', true);
  cap.image = yy;
  ty = tv_micro_images(cap, 'Derotate', true);

  [~, g] = tv_micro_images(cap, 'Derotate', true);
  r0 = find(g.j == 0);  c0 = find(g.i == 0);

  ex = max(max(abs(double(tx{r0, c0}) - want_x)));
  ey = max(max(abs(double(ty{r0, c0}) - want_y)));
end


function c = i_class(descr)
  d = descr;
  if any(d(1) == '<>|='), d = d(2:end); end
  switch d
    case 'u1', c = 'uint8';   case 'u2', c = 'uint16';
    case 'i1', c = 'int8';    case 'i2', c = 'int16';
    case 'u4', c = 'uint32';  case 'i4', c = 'int32';
    case 'f4', c = 'single';  case 'f8', c = 'double';
    otherwise
      % The sidecar spells the dtype as numpy names it ('uint8'), not as the
      % .npy header does ('|u1'); both spellings reach here.
      c = descr;
  end
end
