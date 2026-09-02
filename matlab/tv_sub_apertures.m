function [subs, uv] = tv_sub_apertures(tiles, varargin)
%TV_SUB_APERTURES  Turn micro-images into sub-aperture images.
%
%   SUBS = TV_SUB_APERTURES(TILES) takes the M-by-N cell of X-by-Y
%   micro-images from TV_MICRO_IMAGES and returns the transpose of that data
%   cube: an X-by-Y cell of M-by-N images.
%
%   [SUBS, UV] = TV_SUB_APERTURES(...) also returns
%     .u, .v      the pixel coordinate within the micro-image that each
%                 sub-aperture image was taken from, measured from the tile
%                 centre, so (0,0) is the on-axis view
%     .size       [M N], the size of every sub-aperture image
%
%   Options (name/value):
%     'Stack'   false (default) | true
%               Return an M-by-N-by-X-by-Y numeric array instead of a cell.
%               That is the light field L(j, i, v, u) in one variable, which is
%               what you want the moment you start doing arithmetic across
%               views rather than looking at them.
%
%   WHY BOTH EXIST, since the data is identical and only the indexing differs.
%
%   A MICRO-IMAGE is what one lenslet puts on the sensor: a little picture of
%   the main lens's exit pupil, X by Y pixels, and there are M*N of them. It is
%   the raw structure of the sensor and it is what you look at to judge focus,
%   pitch and alignment.
%
%   A SUB-APERTURE IMAGE is built by taking the SAME pixel out of every
%   micro-image: pixel (u,v) from all M*N lenslets, assembled in lattice order.
%   It is an M-by-N picture of the scene through one small patch of the main
%   aperture -- geometrically an ordinary pinhole camera, which is the whole
%   reason it matters. A calibration model has a projection matrix for a
%   pinhole camera; it has nothing for a micro-image. So the fit consumes
%   sub-apertures, and X*Y of them are available from a single exposure, each
%   with a slightly different centre of projection.
%
%   The trade is resolution: a sub-aperture image is only as large as the
%   lenslet array, here on the order of 13 x 9. That is small, and it is why
%   the calibration target has to put detectable structure inside a MICRO-image
%   rather than relying on the sub-aperture views being detailed.
%
%   The two are one PERMUTE apart. Nothing is interpolated, nothing is lost,
%   and TV_SUB_APERTURES(TV_SUB_APERTURES(T)) is T again.
%
%   VALIDITY. Every sub-aperture image has the same pixel layout as the lattice,
%   so the mask from TV_MICRO_IMAGES applies to all of them unchanged:
%   SUBS{v,u}(r,c) is meaningful exactly where G.VALID(r,c) is true. On a
%   rotated array the corners of the bounding box are never valid, and they
%   appear as black corners in every sub-aperture view. Mask before averaging:
%       s = double(subs{v,u});  s(~g.valid) = NaN;  m = mean(s(:), 'omitnan');
%
%   Example:
%     cap   = tv_read_capture('raw_left_000001.npy');
%     tiles = tv_micro_images(cap);            % 9 x 13 cell of 100 x 100
%     subs  = tv_sub_apertures(tiles);         % 100 x 100 cell of 9 x 13
%     imshow(subs{50, 50}, []);                % the on-axis pinhole view
%     L = tv_sub_apertures(tiles, 'Stack', true);   % 9 x 13 x 100 x 100
%
%   See also TV_MICRO_IMAGES, TV_READ_CAPTURE.

  opt = struct('Stack', false);
  if mod(numel(varargin), 2) ~= 0
    error('tv_sub_apertures:options', 'options must be name/value pairs');
  end
  for k = 1:2:numel(varargin)
    if ~strcmpi(varargin{k}, 'Stack')
      error('tv_sub_apertures:option', 'unknown option "%s"', num2str(varargin{k}));
    end
    opt.Stack = logical(varargin{k + 1});
  end

  if ~iscell(tiles) || isempty(tiles)
    error('tv_sub_apertures:usage', ...
          'first argument must be the cell array from tv_micro_images');
  end

  [M, N] = size(tiles);
  sz = size(tiles{1, 1});
  cls = class(tiles{1, 1});
  for k = 1:numel(tiles)
    if ~isequal(size(tiles{k}), sz)
      error('tv_sub_apertures:ragged', ...
            ['micro-image %d is %s but the first is %s. Every tile must be ' ...
             'the same size for the permute to be defined -- call ' ...
             'tv_micro_images with its default ''Indices'',''whole''.'], ...
            k, mat2str(size(tiles{k})), mat2str(sz));
    end
  end
  Y = sz(1);  X = sz(2);

  % Assemble the light field once: L(j, i, v, u).
  L = zeros(M, N, Y, X, cls);
  for r = 1:M
    for c = 1:N
      L(r, c, :, :) = reshape(tiles{r, c}, [1 1 Y X]);
    end
  end

  if opt.Stack
    subs = L;
  else
    subs = cell(Y, X);
    for v = 1:Y
      for u = 1:X
        subs{v, u} = reshape(L(:, :, v, u), [M N]);
      end
    end
  end

  if nargout > 1
    uv = struct('u', (0:X-1) - (X - 1) / 2, ...
                'v', (0:Y-1) - (Y - 1) / 2, ...
                'size', [M N]);
  end
end
