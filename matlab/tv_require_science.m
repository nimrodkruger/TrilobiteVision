function tv_require_science(cap, what, kind)
%TV_REQUIRE_SCIENCE  Refuse to measure pixels the rig did not vouch for.
%
%   TV_REQUIRE_SCIENCE(CAP) errors unless CAP is an admitted science capture.
%   TV_REQUIRE_SCIENCE(CAP, WHAT) names the operation in the message.
%   TV_REQUIRE_SCIENCE(CAP, WHAT, KIND) picks which question is being asked:
%
%     'values'    (default) reading the pixel VALUES as sensor counts --
%                 radiometry, flat fields, anything where a number means a
%                 photon count. Requires validity 'science' AND the admission
%                 record that claim rests on.
%     'geometry'  reading WHERE things are -- corners, micro-image centres,
%                 lattice fits. Refuses ONLY 'diagnostic', where the rig
%                 established the bytes are not pixel values at all and there
%                 is no reading to measure. Everything else passes with a
%                 warning naming what is unestablished.
%
%   IT FAILS CLOSED, WHICH IS THE CHANGE.
%
%   The first version of this function asked one question -- is the validity
%   string exactly 'diagnostic'? -- and let everything else through. A missing
%   field, a typo, and a value from a schema this code has never seen all read
%   as measurable, on precisely the field whose job is to refuse. So the test
%   is now for an explicitly recognised claim plus its evidence, and anything
%   else is refused by default.
%
%   Pre-boundary archive files have no validity field at all. They are never
%   'science' -- nothing in such a file establishes that anyone ever looked --
%   but they ARE readable, so geometry still works on them and the warning says
%   what is missing. Refusing to open them would be protection against nothing
%   and would train you to route around this function.
%
%   WHY THIS IS A SEPARATE CHECK FROM .space
%
%   .space == 'raw' says the ISP was bypassed. That is a claim about the PATH
%   the pixels took, not about what the values mean. On a Pi 5 the default raw
%   format for the mono IMX296 is MONO_PISP_COMP1 -- the imaging pipeline's
%   COMPRESSED transport. A buffer in it is 'raw', has the right shape, and has
%   obvious structure, so it looks like a picture that has gone slightly wrong
%   rather than like a decode failure. A whole recording session was fitted
%   before anyone noticed.
%
%   See also TV_READ_CAPTURE, TV_MICRO_IMAGES.

  if nargin < 2 || isempty(what)
    what = 'this measurement';
  end
  if nargin < 3 || isempty(kind)
    kind = 'values';
  end
  if ~any(strcmp(kind, {'values', 'geometry'}))
    error('tv_require_science:usage', ...
          'kind must be ''values'' or ''geometry'', not "%s"', kind);
  end
  if ~isstruct(cap)
    error('tv_require_science:usage', 'expects a tv_read_capture struct');
  end

  validity = i_field(cap, 'validity', 'unknown');
  source   = i_field(cap, 'source_kind', 'unknown');
  admitted = false;
  if isfield(cap, 'sensor') && isstruct(cap.sensor) && ...
     isfield(cap.sensor, 'raw_admitted')
    admitted = logical(cap.sensor.raw_admitted);
  end

  is_science  = strcmp(validity, 'science') && admitted;
  % GEOMETRY refuses exactly one thing: 'diagnostic', which is a positive
  % statement that the bytes are not pixel values, so there is no reading of
  % them to measure. Everything else is allowed with a warning.
  %
  % That is deliberately loose. An earlier version refused every case it could
  % not positively vouch for, which stopped archive files and unreconciled raw
  % frames from being looked at at all -- protection against nothing, since a
  % wrong stride skews the aspect ratio and a wrong alignment is 64x too
  % bright, and both are found by LOOKING. Values stay strict, because reading
  % a value as a photon count when the alignment is unresolved is silently
  % wrong and nothing downstream catches it.
  geometry_ok = ~strcmp(validity, 'diagnostic');

  if strcmp(kind, 'values') && is_science
    return;
  end
  if strcmp(kind, 'geometry') && geometry_ok
    if ~is_science
      warning('tv_require_science:notAdmitted', ...
              ['%s is not admitted science data (%s). Corner POSITIONS are ' ...
               'still meaningful; the pixel VALUES are not.'], ...
              i_field(cap, 'tag', 'this capture'), i_why(cap, validity, source));
    end
    return;
  end

  error('tv_require_science:refused', ...
        ['refusing %s.\n  %s\nRe-capture with an admissible raw format; ' ...
         'scripts/probe_cameras.py lists what the sensor offers.'], ...
        what, i_why(cap, validity, source));
end


function why = i_why(cap, validity, source)
%I_WHY  Which of the several different failures this is. They call for
%   different responses and lumping them together is what made the old
%   message useless.
  sensor = struct();
  if isfield(cap, 'sensor') && isstruct(cap.sensor)
    sensor = cap.sensor;
  end
  switch validity
    case 'diagnostic'
      why = sprintf('the rig established these bytes are not pixel values: %s', ...
                    i_field(sensor, 'raw_refusal', 'reason not recorded'));
    case 'science'
      why = ['the sidecar claims ''science'' but carries no admission ' ...
             'record, and a label is not the evidence'];
    case 'unvalidated'
      res = i_field(sensor, 'raw_reservations', {});
      if iscell(res) && ~isempty(res)
        why = strjoin(cellfun(@(s) char(s), res(:)', 'UniformOutput', false), '; ');
      else
        why = sprintf('nothing was established (source: %s)', source);
      end
    otherwise
      why = ['no recognised validity is recorded -- the file predates the ' ...
             'admission boundary, or came from something else'];
  end
end


function v = i_field(s, name, default)
  v = default;
  if isstruct(s) && isfield(s, name) && ~isempty(s.(name))
    v = s.(name);
  end
end
