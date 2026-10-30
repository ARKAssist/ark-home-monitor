"""Constants for ARK Home Monitor."""

DOMAIN = "ark_home"
VERSION = "1.1.1"
API_BASE = "https://arkhome.uk/api/public/ha"

CONF_SYSTEM_ID = "system_id"
CONF_SECRET = "secret"
CONF_ALLOW_RESTART = "allow_restart"

CHECKIN_INTERVAL_SECONDS = 300
MONITORED_DOMAINS = {
    "camera",
    "binary_sensor",
    "sensor",
    "alarm_control_panel",
    "lock",
    "light",
    "switch",
    "climate",
    "cover",
}
LOW_BATTERY_REPORT_PCT = 25
MAX_ERROR_LINES = 50
