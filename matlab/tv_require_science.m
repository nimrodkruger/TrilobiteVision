function tv_require_science(cap, what)
%TV_REQUIRE_SCIENCE  Refuse to measure pixels the rig would not vouch for.
%
%   TV_REQUIRE_SCIENCE(CAP) errors if CAP is tagged 'diagnostic'.
%   TV_REQUIRE_SCIENCE(CAP, WHAT) names the operation in the message.
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
%   So the rig now decides at capture time whether it can establish that a
%   buffer is sensor counts -- known format, uncompressed, unpacked, geometry
%   reconciling with the sensor, values inside the declared bit depth -- and
%   records the verdict as .validity. This is the function that consumes it.
%
%   'unrecorded' (a file written before the boundary existed) is allowed
%   through: refusing the whole archive would be worse than the risk, and those
%   captures were checked by hand at the time. It is not silently promoted to
%   'science' either -- tv_read_capture reports it as unrecorded.
%
%   See also TV_READ_CAPTURE, TV_MICRO_IMAGES.

  if nargin < 2 || isempty(what)
    what = 'this measurement';
  end
  if ~isstruct(cap)
    error('tv_require_science:usage', 'expects a tv_read_capture struct');
  end

  validity = 'unrecorded';
  if isfield(cap, 'validity')
    validity = cap.validity;
  end
  if ~strcmp(validity, 'diagnostic')
    return;
  end

  reason = 'no reason recorded';
  if isfield(cap, 'sensor') && isstruct(cap.sensor) && ...
     isfield(cap.sensor, 'raw_refusal')
    reason = cap.sensor.raw_refusal;
  end

  error('tv_require_science:diagnostic', ...
        ['this capture is tagged DIAGNOSTIC, so %s would be fitting a model ' ...
         'to values that are not sensor counts.\n  reason: %s\n' ...
         'Re-capture with an admissible raw format; ' ...
         'scripts/probe_cameras.py lists what the sensor offers.'], ...
        what, reason);
end
