/** @type {import('next').NextConfig} */
const backend = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig = {
  output: "standalone",
  async rewrites() {
    // Proxy the API through the frontend origin so httpOnly auth cookies are first-party.
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      { source: "/healthz", destination: `${backend}/healthz` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
