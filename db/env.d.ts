declare namespace Cloudflare {
  interface Env {
    DB: D1Database;
    GROQ_API_KEY?: string;
    GROQ_MODEL?: string;
  }
}
