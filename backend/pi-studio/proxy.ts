import { NextResponse, type NextRequest } from "next/server";
import { getBioMindProjectRoots, isBioMindApiAllowed, isBioMindRemoteProfile, isBioMindRequestAllowed } from "@/lib/remote-security";
import {
  isApiRequestAllowed,
  isApiRequestHostAllowed,
} from "@/lib/request-security";
import {
  isValidBasicAuthorization,
  isWebPasswordEnabled,
} from "@/lib/web-auth";

export function proxy(request: NextRequest) {
  const isApiRequest = request.nextUrl.pathname === "/api"
    || request.nextUrl.pathname.startsWith("/api/");
  if (isBioMindRemoteProfile()) {
    if (!isWebPasswordEnabled(process.env.PI_WEB_PASSWORD)) return NextResponse.json({ error: "BIOMIND_AUTH_NOT_CONFIGURED" }, { status: 503 });
    try { getBioMindProjectRoots(); } catch { return NextResponse.json({ error: "BIOMIND_ROOTS_NOT_CONFIGURED" }, { status: 503 }); }
    if (!isBioMindRequestAllowed(request)) return NextResponse.json({ error: "Untrusted request" }, { status: 403 });
    if (isApiRequest && !isBioMindApiAllowed(request.nextUrl.pathname, request.method, request.nextUrl.searchParams)) {
      return NextResponse.json({ error: "BIOMIND_PROFILE_OPERATION_UNAVAILABLE" }, { status: 403 });
    }
  }
  const isTrustedRequest = isApiRequest
    ? isApiRequestAllowed(request)
    : isApiRequestHostAllowed(request);

  if (!isTrustedRequest) {
    if (!isApiRequest) {
      return new NextResponse("Untrusted request", { status: 403 });
    }
    return NextResponse.json({ error: "Untrusted API request" }, { status: 403 });
  }

  const password = process.env.PI_WEB_PASSWORD;
  if (
    isWebPasswordEnabled(password)
    && !isValidBasicAuthorization(request.headers.get("authorization"), password)
  ) {
    return new NextResponse("Authentication required", {
      status: 401,
      headers: {
        "Cache-Control": "no-store",
        "WWW-Authenticate": 'Basic realm="Pi Studio", charset="UTF-8"',
      },
    });
  }

  return NextResponse.next();
}

// Protect every page and asset too: no alternative page can bypass auth.
export const config = { matcher: ["/:path*"] };
