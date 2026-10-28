from secure import Secure

from app.config import settings
from secure.headers import (
    ContentSecurityPolicy,
    PermissionsPolicy,
    XFrameOptions,
    CrossOriginOpenerPolicy,
    CrossOriginResourcePolicy,
    StrictTransportSecurity,
    ReferrerPolicy,
    XContentTypeOptions,
)

if settings.environment == "development":
    csp = (
        ContentSecurityPolicy()
        .default_src("'self'").base_uri("'self'").font_src("'self'", "data:")
        .form_action("'self'").frame_ancestors("'self'").img_src("'self'", "data:", "https:")
        .object_src("'none'").script_src("'self'", "'unsafe-inline'", "'unsafe-eval'")
        .script_src_attr("'none'").style_src("'self'", "'unsafe-inline'")
        .connect_src("'self'", "ws:").upgrade_insecure_requests()
        .report_only()
    )
else:
    csp = (
        ContentSecurityPolicy()
        .default_src("'self'").base_uri("'self'").font_src("'self'", "data:")
        .form_action("'self'").frame_ancestors("'self'").img_src("'self'", "data:", "https:")
        .object_src("'none'").script_src("'self'")
        .script_src_attr("'none'").style_src("'self'", "'unsafe-inline'")
        .connect_src("'self'").upgrade_insecure_requests()
        # Phase 96 (CSP-01): production CSP is now ENFORCING — no Report-Only mode.
        # If console CSP violations appear, first add to development CSP, test,
        # then promote to production. Keep in sync with nginx.conf location / CSP.
    )

permissions = (
    PermissionsPolicy()
    .geolocation().microphone().camera().autoplay().display_capture()
    .payment().usb().bluetooth().accelerometer().gyroscope().magnetometer()
)

secure_headers = Secure(
    xfo=XFrameOptions().deny(),
    permissions=permissions,
    csp=csp,
    coop=CrossOriginOpenerPolicy().same_origin(),
    corp=CrossOriginResourcePolicy().same_origin(),
    hsts=StrictTransportSecurity().max_age(31536000).include_subdomains(),
    referrer=ReferrerPolicy().strict_origin_when_cross_origin(),
    xcto=XContentTypeOptions().nosniff(),
)


def get_secure_headers() -> Secure:
    return secure_headers
