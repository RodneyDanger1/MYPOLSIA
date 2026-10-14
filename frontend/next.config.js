/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  env: {
    // Deployment-grade default (2026-10-13 live pass): RELATIVE base — the browser then calls
    // /api/v1 on whatever origin serves the app (the nginx mux), the only shape that works from
    // 127.0.0.1, localhost, a LAN IP and a hostname alike.  The old absolute `http://localhost`
    // default broke every access path except same-machine `localhost` (and turned 127.0.0.1 loads
    // into CORS-blocked cross-origin fetches — measured live).  Explicit build-time env (README's
    // no-Docker run, e2e lab) still overrides.
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "",
    NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL || "",
  },
};

module.exports = nextConfig;
