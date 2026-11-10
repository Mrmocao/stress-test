"""Evidence limits. Unsupported SDK behavior must stay explicit."""

CAPABILITIES = {
    'guest_refresh': {'status': 'unsupported', 'reason': 'No verified acquisition/refresh contract'},
    'complex_extra_escaping': {'status': 'unsupported', 'reason': 'No verified SDK escaping grammar'},
    'trace_headers': {'status': 'unsupported', 'reason': 'Trace source and lifetime not verified'},
    'aurora': {'status': 'unsupported', 'reason': 'SDK identity source not verified'},
    'ad_telemetry': {'status': 'unsupported', 'reason': 'Ad and binary telemetry bodies not decoded'},
    'interactive_player': {'status': 'offline_model_only', 'reason': 'FFmpeg adapter has no pause/seek/background controls'},
    'tv_lifecycle': {'status': 'offline_verified', 'reason': 'No current public acceptance regression'},
}
