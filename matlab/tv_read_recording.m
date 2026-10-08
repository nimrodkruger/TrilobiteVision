function rec = tv_read_recording(directory)
%TV_READ_RECORDING  Open a TrilobiteVision recording directory in MATLAB.
%
%   REC = TV_READ_RECORDING(DIR) opens a recording written by Stage 5 -- a
%   burst or a continuous run -- and returns a struct describing it. It reads
%   the journal and every chunk index; it does NOT read any pixels. Use
%   TV_RECORDING_FRAME to pull frames out one at a time.
%
%   A recording is not a capture, and the difference is the reason this exists
%   as a separate function rather than an option on TV_READ_CAPTURE:
%
%     a capture    one .npy plus one .json sidecar. Pixels already oriented.
%     a recording  a directory: recording.json (the journal), then one
%                  subdirectory per head holding rec_<cam>_NNNNN.npy chunk
%                  files, each with a rec_<cam>_NNNNN.index.json beside it.
%                  Pixels are stored in the SENSOR frame and this reader turns
%                  them, because a quarter turn is a full-array copy and doing
%                  it at 30 fps on the Pi would cost bandwidth the recording
%                  needs. See .orientation below.
%
%   THE FIELD TO READ FIRST IS .frames_dropped.
%
%   A recording is allowed to lose frames. When the write path cannot keep up,
%   whole frames are dropped -- never bit depth, never geometry, never the
%   frame rate, all of which are fixed before the recording starts. So every
%   frame in the files has the same shape and depth, and the loss is temporal:
%   the stored frames are an irregularly sampled subset of the exposures.
%
%   This is why the row index in a chunk is NOT a time axis. Use .seq (the
%   sequence number as exposed) and .sensor_timestamp from each frame's index
%   record. Treating row k as "the k-th frame" silently compresses every gap.
%
%   Returned struct:
%
%     .path           the directory
%     .journal        the decoded recording.json
%     .kind           'continuous' or 'burst'
%     .complete       false if the recording was interrupted. A directory with
%                     chunks and no journal is an interrupted recording and
%                     errors here rather than reading as a whole one.
%     .plan           the configuration chosen before Start: .format_key,
%                     .fps, .width, .height, .validity_ceiling, ...
%     .cams           cell array of head names, sorted
%     .heads          struct keyed by head name; each has
%                       .chunks      struct array, one per chunk file, with
%                                    .npy, .index_file, .shape, .dtype,
%                                    .header_bytes, .frame_bytes, .frames
%                                    (the index records) and .gaps
%                       .stored      frames on disk, counted from the indexes
%                       .exposed     last seq - first seq + 1
%                       .dropped     exposed - stored
%                       .unaccounted stored + dropped - exposed. MUST be 0.
%                       .gaps        [first last] rows of missing seq numbers
%                       .longest_gap_frames
%                       .claimed     what the journal said, for comparison
%                       .agrees      whether the two match
%     .orientation    struct keyed by head: the transform TV_RECORDING_FRAME
%                     will apply. Empty when the backend already applied it.
%     .every_frame_accounted  recomputed here from the files, not copied from
%                     the journal. The journal says what the rig believed; this
%                     says what is on the disk. Stage 5c's acceptance criterion
%                     is that the two agree, so they are kept separate: see
%                     .heads.<cam>.agrees.
%
%   Example:
%
%     rec = tv_read_recording('E:\trilobite\session_x\recording_raw16_20fps');
%     fprintf('%d stored, %d dropped\n', ...
%             rec.heads.left.stored, rec.heads.left.dropped);
%     [img, meta] = tv_recording_frame(rec, 'left', 1);
%     imagesc(img); axis image; colormap gray;
%     title(sprintf('seq %d', meta.seq));
%
%   See also TV_RECORDING_FRAME, TV_READ_CAPTURE, TV_READ_NPY.

  if nargin < 1 || isempty(directory)
    error('tv_read_recording:usage', 'tv_read_recording(directory)');
  end
  if exist(directory, 'dir') ~= 7
    error('tv_read_recording:notFound', 'no such directory: %s', directory);
  end

  % -- the journal, and the schema gate ---------------------------------
  %
  % Refused rather than half-read. A newer writer may have changed what a
  % chunk or an index means, and a reader that guesses produces numbers that
  % look right. This is the same gate TV_READ_CAPTURE applies to the still
  % sidecar, and deliberately a SEPARATE version number: a reader may
  % understand one and not the other.
  SUPPORTED = 1;

  jpath = fullfile(directory, 'recording.json');
  if exist(jpath, 'file') ~= 2
    error('tv_read_recording:noJournal', ...
          ['%s has no recording.json. The journal is written LAST, so a ' ...
           'directory of chunks without one is an interrupted recording. ' ...
           'The chunks may still be readable with tv_read_npy, but nothing ' ...
           'here can tell you what is missing from them.'], directory);
  end
  journal = jsondecode(fileread(jpath));

  schema = i_get(journal, 'schema', []);
  if ~isempty(schema) && double(schema) > SUPPORTED
    error('tv_read_recording:schema', ...
          ['recording schema %d, and this reader understands up to %d. ' ...
           'Refused rather than half-read -- update the matlab/ folder from ' ...
           'the commit that wrote this recording.'], double(schema), SUPPORTED);
  end

  rec = struct();
  rec.path = char(directory);
  rec.journal = journal;
  rec.kind = char(i_get(journal, 'kind', 'unknown'));
  % A burst journal carries no 'complete' key: its existence IS completion,
  % because a burst is written in one pass that either finished or did not.
  rec.complete = logical(i_get(journal, 'complete', strcmp(rec.kind, 'burst')));
  rec.plan = i_get(journal, 'plan', struct());
  rec.stop_reason = char(i_get(journal, 'stop_reason', ''));

  % -- orientation: applied or to be applied ----------------------------
  pixels_oriented = logical(i_get(rec.plan, 'pixels_oriented', false));
  rec.pixels_oriented = pixels_oriented;
  plan_orient = i_get(rec.plan, 'orientation', struct());

  % -- the heads --------------------------------------------------------
  claimed_heads = i_get(journal, 'heads', struct());
  cams = i_subdirs(directory);
  if isempty(cams) && isstruct(claimed_heads)
    cams = fieldnames(claimed_heads);
  end
  cams = sort(cams);

  rec.cams = cams;
  rec.heads = struct();
  rec.orientation = struct();
  unaccounted_total = 0;

  for k = 1:numel(cams)
    cam = cams{k};
    head = i_read_head(fullfile(directory, cam), cam);
    head.claimed = i_get(claimed_heads, cam, struct());
    head.agrees = i_agrees(head);
    rec.heads.(cam) = head;
    unaccounted_total = unaccounted_total + head.unaccounted;

    if pixels_oriented
      rec.orientation.(cam) = struct();   % nothing left to do
    else
      rec.orientation.(cam) = i_orient_spec(i_get(plan_orient, cam, struct()));
    end
  end

  rec.every_frame_accounted = (unaccounted_total == 0);
  rec.frames_stored = i_sum_field(rec.heads, cams, 'stored');
  rec.frames_exposed = i_sum_field(rec.heads, cams, 'exposed');
  rec.frames_dropped = i_sum_field(rec.heads, cams, 'dropped');
  if rec.frames_exposed > 0
    rec.drop_fraction = rec.frames_dropped / rec.frames_exposed;
  else
    rec.drop_fraction = 0;
  end

  % No trigger couples the heads and none is claimed. They drop
  % independently, so row k of 'left' and row k of 'right' are not a pair:
  % reconcile them from .sensor_timestamp, in each head's own clock domain.
  rec.synchronised = false;
end


% ----------------------------------------------------------------------
function head = i_read_head(dir_path, cam)
%I_READ_HEAD  Every chunk index for one head, and the accounting they imply.

  head = struct('chunks', struct([]), 'stored', 0, 'exposed', 0, ...
                'dropped', 0, 'unaccounted', 0, 'gaps', zeros(0, 2), ...
                'longest_gap_frames', 0, 'first_seq', [], 'last_seq', []);
  if exist(dir_path, 'dir') ~= 7
    return;
  end

  listing = dir(fullfile(dir_path, '*.index.json'));
  if isempty(listing)
    return;
  end
  [~, order] = sort({listing.name});
  listing = listing(order);

  chunks = struct('npy', {}, 'index_file', {}, 'shape', {}, 'dtype', {}, ...
                  'header_bytes', {}, 'frame_bytes', {}, 'frames', {}, ...
                  'gaps', {}, 'n', {});
  seqs = [];

  for i = 1:numel(listing)
    ipath = fullfile(dir_path, listing(i).name);
    idx = jsondecode(fileread(ipath));

    c = struct();
    c.index_file = ipath;
    c.npy = fullfile(dir_path, char(i_get(idx, 'file', ...
                                 strrep(listing(i).name, '.index.json', '.npy'))));
    c.shape = double(i_get(idx, 'shape', []))';
    c.dtype = char(i_get(idx, 'dtype', ''));
    c.header_bytes = double(i_get(idx, 'header_bytes', 128));
    c.frame_bytes = double(i_get(idx, 'frame_bytes', 0));
    c.frames = i_get(idx, 'frames', struct([]));
    c.gaps = i_get(idx, 'gaps', struct([]));
    c.n = numel(c.frames);
    chunks(end + 1) = c;                                       %#ok<AGROW>

    seqs = [seqs; i_seqs(c.frames)];                           %#ok<AGROW>
  end

  head.chunks = chunks;
  seqs = sort(seqs);
  head.stored = numel(seqs);
  if ~isempty(seqs)
    head.first_seq = seqs(1);
    head.last_seq = seqs(end);
    head.exposed = seqs(end) - seqs(1) + 1;
    d = diff(seqs);
    jump = find(d > 1);
    head.gaps = [seqs(jump) + 1, seqs(jump + 1) - 1];
    if ~isempty(head.gaps)
      head.longest_gap_frames = max(head.gaps(:, 2) - head.gaps(:, 1) + 1);
    end
    head.dropped = head.exposed - head.stored;
  end
  % The invariant. Every exposed frame is either on the disk or inside a named
  % gap; anything else is a frame that vanished without being counted, which
  % is the one outcome Stage 5c is built to make impossible.
  head.unaccounted = head.stored + head.dropped - head.exposed;
end


function ok = i_agrees(head)
%I_AGREES  Does the journal's claim match what the indexes actually hold?
  ok = true;
  c = head.claimed;
  if ~isstruct(c) || isempty(fieldnames(c))
    ok = false;    % nothing to compare against is not agreement
    return;
  end
  pairs = {'frames_stored', 'stored'; 'frames_exposed', 'exposed'; ...
           'frames_dropped', 'dropped'};
  for i = 1:size(pairs, 1)
    claimed = i_get(c, pairs{i, 1}, []);
    if isempty(claimed) || double(claimed) ~= head.(pairs{i, 2})
      ok = false;
      return;
    end
  end
end


function s = i_orient_spec(raw)
%I_ORIENT_SPEC  Normalise the plan's orientation block for one head.
  s = struct( ...
    'rotate_deg',      double(i_get(raw, 'rotate_deg', 0)), ...
    'flip_horizontal', logical(i_get(raw, 'flip_horizontal', false)), ...
    'flip_vertical',   logical(i_get(raw, 'flip_vertical', false)));
end


function v = i_seqs(frames)
%I_SEQS  Sequence numbers out of a chunk index's frame records.
  v = zeros(0, 1);
  if isempty(frames)
    return;
  end
  if isstruct(frames)
    v = double([frames.seq])';
  elseif iscell(frames)
    v = zeros(numel(frames), 1);
    for i = 1:numel(frames)
      v(i) = double(i_get(frames{i}, 'seq', NaN));
    end
  end
  v = v(~isnan(v));
end


function names = i_subdirs(directory)
  listing = dir(directory);
  names = {};
  for i = 1:numel(listing)
    if listing(i).isdir && ~strcmp(listing(i).name, '.') && ...
       ~strcmp(listing(i).name, '..')
      names{end + 1} = listing(i).name;                        %#ok<AGROW>
    end
  end
  names = names(:);
end


function total = i_sum_field(heads, cams, field_name)
  total = 0;
  for i = 1:numel(cams)
    total = total + heads.(cams{i}).(field_name);
  end
end


function v = i_get(s, name, default)
  if isstruct(s) && isfield(s, name) && ~isempty(s.(name))
    v = s.(name);
  elseif isstruct(s) && isfield(s, name)
    v = s.(name);
    if isempty(v)
      v = default;
    end
  else
    v = default;
  end
end
