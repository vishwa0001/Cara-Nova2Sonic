export const CONFIG = {
  MAX_FILE_SIZE_MB: 10,
  MAX_FILE_SIZE_BYTES: 10 * 1024 * 1024,
  MIN_SCHEDULE_LEAD_TIME_MINUTES: 1,
  POLL_INTERVAL_MS: 20_000,
  TIMEZONE: {
    IANA: "Asia/Kolkata",
    DISPLAY: "IST (UTC+05:30)",
  },
  ALLOWED_EXTENSIONS: [".csv"],
  // Cara's voice for the whole call; Joanna is the default.
  DEFAULT_VOICE: "joanna",
  VOICE_OPTIONS: [
    { value: "joanna", label: "Joanna (US, female)" },
    { value: "tiffany", label: "Tiffany (US, female)" },
    { value: "matthew", label: "Matthew (US, male)" },
    { value: "amy", label: "Amy (UK, female)" },
    { value: "olivia", label: "Olivia (Australian, female)" },
    { value: "kiara", label: "Kiara (Indian, female)" },
  ],
  REQUIRED_COLUMNS: [
    "empi",
    "first_name",
    "last_name",
    "gender",
    "phone_number",
    "practice_name",
    "practice_callback_number",
  ],
  API: {
    BASE_URL: (import.meta as any).env?.VITE_CAMPAIGN_API_BASE_URL || "",
    // Backward-compatibility with the previous split Lambda URL prototype.
    LEGACY_UPLOAD_URL:
      (import.meta as any).env?.VITE_UPLOAD_URL_API ||
      (import.meta as any).env?.VITE_INGESTION_API_URL ||
      "",
    LEGACY_LIST_URL: (import.meta as any).env?.VITE_LIST_API_URL || "",
  },
};
