function demo_read_capture(path)
%DEMO_READ_CAPTURE  Worked example: capture on disk -> MATLAB -> sub-apertures.
%
%   DEMO_READ_CAPTURE(PATH) runs the whole chain on one capture and draws four
%   figures. PATH is the .npy or the .json from either camera, view or raw.
%
%   DEMO_READ_CAPTURE with no argument looks for the newest capture under a
%   'trilobite-data' folder beside this file or in the current directory.
%
%   Read this top to bottom once; it is shorter than the documentation and it
%   is the documentation that is executed.

  if nargin < 1 || isempty(path)
    path = i_newest();
    fprintf('using the newest capture found: %s\n', path);
  end

  % ---------------------------------------------------------------------
  % 1. Load. One call gets the pixels and everything known about them.
  % ---------------------------------------------------------------------
  cap = tv_read_capture(path);

  fprintf('\n%s\n', repmat('-', 1, 68));
  fprintf('%s  (%s, %s)\n', cap.files.image, cap.cam_id, cap.tag);
  fprintf('%s\n', repmat('-', 1, 68));
  fprintf('  captured   %s\n', cap.t_iso);
  fprintf('  image      %d x %d  %s  (%s)\n', ...
          cap.width, cap.height, class(cap.image), cap.space);
  fprintf('  range      %g .. %g\n', ...
          double(min(cap.image(:))), double(max(cap.image(:))));
  if isfield(cap.sensor, 'ExposureTime')
    fprintf('  exposure   %g us\n', double(cap.sensor.ExposureTime));
  end
  if isfield(cap.sensor, 'AnalogueGain')
    fprintf('  gain       %g\n', double(cap.sensor.AnalogueGain));
  end

  if ~cap.has_mla
    fprintf('\n  NO MLA GEOMETRY in this capture -- the grid stage was off.\n');
    fprintf('  Showing the frame only; there is nothing to split it into.\n\n');
    figure('Name', 'frame'); i_show(cap.image); title(cap.files.image, ...
      'Interpreter', 'none');
    return;
  end

  % ---------------------------------------------------------------------
  % 2. The geometry, already converted to THIS frame's pixels.
  %    Note the scale factor: the grid was aligned on the preview.
  % ---------------------------------------------------------------------
  m = cap.mla;
  fprintf('\n  MLA, rescaled to this frame (x%.3f from the %dx%d preview):\n', ...
          m.scale_applied, m.reference.reference_width, m.reference.reference_height);
  fprintf('    pitch      %.3f px   (%.3f as recorded)\n', ...
          m.pitch, m.reference.pitch_px);
  fprintf('    rotation   %.3f deg\n', m.rotation_deg);
  fprintf('    offset     %.3f, %.3f px\n', m.offset_x, m.offset_y);
  fprintf('    crop scale %.3f\n', m.crop_scale);

  % ---------------------------------------------------------------------
  % 3. Split into micro-images: N x M cells, each X x Y, de-rotated so the
  %    lattice axes are the tile axes.
  % ---------------------------------------------------------------------
  [tiles, g] = tv_micro_images(cap, 'Derotate', true);
  fprintf('\n  micro-images  %d x %d cells of %d x %d px  (%d complete)\n', ...
          size(tiles, 1), size(tiles, 2), g.side, g.side, sum(g.valid(:)));
  fprintf('    lattice i  %d .. %d\n', min(g.i), max(g.i));
  fprintf('    lattice j  %d .. %d\n', min(g.j), max(g.j));
  fprintf('    derotated  %d\n', g.derotated);

  % ---------------------------------------------------------------------
  % 4. And the sub-apertures: the same numbers, indexed the other way.
  % ---------------------------------------------------------------------
  [subs, uv] = tv_sub_apertures(tiles);
  fprintf('\n  sub-apertures %d x %d views of %d x %d px\n', ...
          size(subs, 1), size(subs, 2), uv.size(1), uv.size(2));
  fprintf('    each one is an ordinary pinhole camera; that is what the\n');
  fprintf('    calibration model can actually fit.\n');
  fprintf('    g.valid masks them all: subs{v,u}(r,c) is meaningful exactly\n');
  fprintf('    where g.valid(r,c) is. On a rotated array the box corners\n');
  fprintf('    never are, and they show as black corners in every view.\n\n');

  % ---------------------------------------------------------------------
  % 5. Look at it.
  % ---------------------------------------------------------------------
  figure('Name', 'frame');
  i_show(cap.image); hold on;
  i_draw_grid(g);
  title(sprintf('%s  %s  %dx%d', cap.cam_id, cap.tag, cap.width, cap.height), ...
        'Interpreter', 'none');

  % The centre micro-image, large. THIS is the picture that settles whether
  % the optics can support the calibration at all: if a checkerboard is not
  % visible here to the eye, no detector will find one.
  [~, rc] = min(abs(g.j));  [~, cc] = min(abs(g.i));
  figure('Name', 'centre micro-image');
  i_show(tiles{rc, cc});
  title(sprintf('micro-image (i=%d, j=%d), %d x %d px', ...
                g.i(cc), g.j(rc), g.side, g.side));

  % A patch of the array, to see how micro-images differ across the aperture.
  r0 = max(1, rc - 2);  r1 = min(size(tiles, 1), rc + 2);
  c0 = max(1, cc - 2);  c1 = min(size(tiles, 2), cc + 2);
  figure('Name', 'micro-images, 5x5 about the centre');
  i_show(cell2mat(tiles(r0:r1, c0:c1)));
  title('micro-images i-2..i+2, j-2..j+2');

  % The on-axis sub-aperture: a small, ordinary photograph of the scene.
  [~, vu] = min(abs(uv.v));  [~, uu] = min(abs(uv.u));
  figure('Name', 'on-axis sub-aperture');
  i_show(subs{vu, uu});
  title(sprintf('sub-aperture u=%g v=%g  (%d x %d)', ...
                uv.u(uu), uv.v(vu), uv.size(1), uv.size(2)));
end


function i_show(img)
%I_SHOW  Display one image, grey, square pixels, percentile-stretched.
%   Uses base graphics only. imshow lives in the Image Processing Toolbox in
%   MATLAB and in the image package in Octave, neither of which is worth
%   requiring to look at a frame.
  imagesc(i_stretch(img));
  colormap(gray(256)); axis image; axis off; caxis([0 1]);
end


function out = i_stretch(img)
%I_STRETCH  Percentile stretch for display only. Never for measurement.
%   Raw frames are 10- or 12-bit in a 16-bit container, so the top four to six
%   bits are empty and an unstretched imshow looks black.
  v = double(img(:));
  lo = prctile_(v, 1);  hi = prctile_(v, 99.5);
  if hi <= lo
    lo = min(v);  hi = max(v);
  end
  if hi <= lo
    out = zeros(size(img));  return;
  end
  out = (double(img) - lo) / (hi - lo);
  out = min(max(out, 0), 1);
end


function p = prctile_(v, q)
%PRCTILE_  Percentile without the Statistics Toolbox.
  v = sort(v(~isnan(v)));
  if isempty(v), p = NaN; return; end
  idx = min(max(round(q / 100 * numel(v)), 1), numel(v));
  p = v(idx);
end


function i_draw_grid(g)
  side = g.side;
  for r = 1:numel(g.j)
    for c = 1:numel(g.i)
      if ~g.valid(r, c), continue; end
      x = g.centres(r, c, 1) - side / 2 + 1;    % +1: 0-based -> plot coords
      y = g.centres(r, c, 2) - side / 2 + 1;
      rectangle('Position', [x y side side], 'EdgeColor', [0.2 0.9 0.5], ...
                'LineWidth', 0.5);
    end
  end
end


function p = i_newest()
  roots = {fullfile(fileparts(mfilename('fullpath')), '..', 'trilobite-data'), ...
           fullfile(pwd, 'trilobite-data'), pwd};
  for k = 1:numel(roots)
    if exist(roots{k}, 'dir') ~= 7, continue; end
    f = i_find_npy(roots{k}, 0);
    if ~isempty(f)
      [~, order] = sort([f.datenum], 'descend');
      p = fullfile(f(order(1)).folder, f(order(1)).name);
      return;
    end
  end
  error('demo_read_capture:noCapture', ...
        ['no .npy capture found. Pass the path explicitly, e.g.\n' ...
         '  demo_read_capture(''trilobite-data/session_.../left/raw_left_000001.npy'')']);
end


function out = i_find_npy(root, depth)
  out = struct('name', {}, 'folder', {}, 'datenum', {});
  if depth > 4, return; end
  d = dir(root);
  for k = 1:numel(d)
    if strcmp(d(k).name, '.') || strcmp(d(k).name, '..'), continue; end
    p = fullfile(root, d(k).name);
    if d(k).isdir
      out = [out, i_find_npy(p, depth + 1)];  %#ok<AGROW>
    elseif numel(d(k).name) > 4 && strcmpi(d(k).name(end-3:end), '.npy')
      e = struct('name', d(k).name, 'folder', root, 'datenum', d(k).datenum);
      out = [out, e];  %#ok<AGROW>
    end
  end
end
