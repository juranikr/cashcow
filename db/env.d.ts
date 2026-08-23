declare namespace Cloudflare {
  interface Env {
    DB: D1Database;
    GROQ_API_KEY?: string;
    GROQ_MODEL?: string;
    RUNTIME_BASE_URL?: string;
    RUNTIME_SERVICE_TOKEN?: string;
  }
}
