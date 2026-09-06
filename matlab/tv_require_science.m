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
%                 lattice fits. An ISP mono frame is a geometrically faithful
%                 picture of the scene, so 'unvalidated' output from an ISP
%                 source passes; every calibration pose is exactly that.
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
%   Pre-boundary archive files have no validity field at all. They are refused
%   rather than assumed to have been checked: nothing in such a file
%   establishes that anyone ever looked. Inspect them deliberately with
%   TV_READ_CAPTURE and your own judgement, not by having this function shrug.
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
  isp_source  = any(strcmp(source, {'isp_main', 'isp_lores'}));
  geometry_ok = is_science || (strcmp(validity, 'unvalidated') && isp_source);

  if (strcmp(kind, 'values') && is_science) || ...
     (strcmp(kind, 'geometry') && geometry_ok)
    return;
  end

  % Say which of the several different failures this is. They call for
  % different responses and lumping them together is what made the old message
  % useless.
  switch validity
    case 'diagnostic'
      why = sprintf(['the rig REFUSED this buffer: %s'], ...
                    i_field(cap.sensor, 'raw_refusal', 'reason not recorded'));
    case 'science'
      why = ['the sidecar claims ''science'' but carries no admission ' ...
             'record. A label is not the evidence.'];
    case 'unvalidated'
      why = sprintf(['nothing was established about these values (source: ' ...
                     '%s). ISP output is geometrically faithful but its ' ...
                     'values are not sensor counts.'], source);
    otherwise
      why = ['no recognised validity is recorded. The file either predates ' ...
             'the admission boundary or was written by something other than ' ...
             'this rig.'];
  end

  error('tv_require_science:refused', ...
        ['refusing %s.\n  %s\nRe-capture with an admissible raw format; ' ...
         'scripts/probe_cameras.py lists what the sensor offers. To look at ' ...
         'it anyway, call the lower-level functions directly -- looking is ' ...
         'not measuring.'], what, why);
end


function v = i_field(s, name, default)
  v = default;
  if isstruct(s) && isfield(s, name) && ~isempty(s.(name))
    v = s.(name);
  end
end
