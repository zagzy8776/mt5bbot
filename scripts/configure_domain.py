"""Configure Caddy for HTTPS with a real domain.

Usage: python configure_domain.py api.example.com

This script:
1. Updates deploy/caddy/Caddyfile with the domain
2. Restarts Caddy to provision Let's Encrypt HTTPS
3. Reports what to set in Vercel and .env

Prerequisites:
- DNS A record: api.<YOURDOMAIN> -> 13.53.232.117
- Caddy running (start with: C:\caddy.exe run --config C:\mt5bbot\deploy\caddy\Caddyfile)
"""

import sys
from pathlib import Path

CADDYFILE = Path(r"C:\mt5bbot\deploy\caddy\Caddyfile")
ENV_FILE = Path(r"C:\mt5bbot\.env")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python configure_domain.py api.example.com")
        print("  api.example.com must resolve to 13.53.232.117")
        return 1

    domain = sys.argv[1].strip()
    if domain.startswith("api."):
        domain = domain[4:]
    api_domain = f"api.{domain}"

    print(f"Domain: {domain}")
    print(f"API domain: {api_domain}")
    print()

    # Update Caddyfile
    content = CADDYFILE.read_text(encoding="utf-8")
    content = content.replace("api.<YOURDOMAIN>", api_domain)
    CADDYFILE.write_text(content, encoding="utf-8")
    print(f"1. Updated {CADDYFILE} with {api_domain}")

    # Update CORS_ORIGINS in .env if file exists
    if ENV_FILE.exists():
        env_content = ENV_FILE.read_text(encoding="utf-8")
        lines = env_content.split("\n")
        new_lines = []
        for line in lines:
            if line.startswith("CORS_ORIGINS="):
                # Add the Vercel URL if not already present
                if "frontend-three-eta-53.vercel.app" not in line:
                    new_lines.append(
                        "CORS_ORIGINS=https://frontend-three-eta-53.vercel.app,"
                        f"{line.split('=', 1)[1]}"
                    )
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        ENV_FILE.write_text("\n".join(new_lines), encoding="utf-8")
        print(f"2. Updated CORS_ORIGINS in {ENV_FILE}")
    else:
        print("2. Skipped .env update (file not found)")

    print()
    print("=" * 60)
    print("NEXT STEPS:")
    print("=" * 60)
    print(f"3. Verify DNS resolves:  nslookup {api_domain}")
    print("   Expected: 13.53.232.117")
    print()
    print("4. Restart Caddy:")
    print("   Stop old:  taskkill /IM caddy.exe /F")
    print(f"   Start new: C:\\caddy.exe run --config {CADDYFILE}")
    print()
    print("5. Verify HTTPS:")
    print(f"   curl https://{api_domain}/health")
    print()
    print("6. Set Vercel env var:")
    print(f"   VITE_API_BASE_URL = https://{api_domain}")
    print()
    print("7. Redeploy Vercel")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
