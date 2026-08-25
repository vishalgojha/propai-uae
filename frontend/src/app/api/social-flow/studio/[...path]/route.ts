import { NextRequest, NextResponse } from "next/server";

const SDK_BASE = (process.env.SOCIAL_FLOW_SDK_URL || "").replace(/\/$/, "");

function patchStudioScript(source: string) {
  return source
    .replace(
      'var baseUrl = (window.location.protocol + "//" + window.location.host).replace(/\\/$/, "");',
      process.env.NEXT_PUBLIC_BASE_PATH
        ? `var baseUrl = window.location.origin + "${process.env.NEXT_PUBLIC_BASE_PATH}/api/social-flow";`
        : 'var baseUrl = window.location.origin + "/api/social-flow";'
    )
    .replace(
      'headers: options.body ? { "Content-Type": "application/json" } : undefined,',
      'headers: Object.assign(options.body ? { "Content-Type": "application/json" } : {}, (window.sessionStorage.getItem("propai_social_flow_token") ? { Authorization: "Bearer " + window.sessionStorage.getItem("propai_social_flow_token") } : {})),',
    );
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  if (!SDK_BASE) return new NextResponse("Social Flow is not configured", { status: 503 });

  const path = (await context.params).path || [];
  const target = `${SDK_BASE}/${path.join("/")}${new URL(request.url).search}`;
  const response = await fetch(target, { cache: "no-store" });
  const contentType = response.headers.get("content-type") || "application/octet-stream";

  if (path.at(-1) === "app.js" && contentType.includes("javascript")) {
    return new NextResponse(patchStudioScript(await response.text()), {
      status: response.status,
      headers: { "Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  return new NextResponse(response.body, {
    status: response.status,
    headers: { "Content-Type": contentType, "Cache-Control": "no-store" },
  });
}
