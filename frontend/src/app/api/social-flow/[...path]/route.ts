import { NextRequest, NextResponse } from "next/server";
import { getSupabaseAdmin } from "@/lib/supabase-admin";

const SDK_BASE = (process.env.SOCIAL_FLOW_SDK_URL || "").replace(/\/$/, "");
const SDK_KEY = process.env.SOCIAL_FLOW_SDK_API_KEY || "";

function patchStudioScript(source: string) {
  return source
    .replace(
      'var baseUrl = (window.location.protocol + "//" + window.location.host).replace(/\\/$/, "");',
      process.env.NEXT_PUBLIC_BASE_PATH
        ? `var baseUrl = window.location.origin + "${process.env.NEXT_PUBLIC_BASE_PATH}/api/social-flow";`
        : 'var baseUrl = window.location.origin + "/api/social-flow";',
    )
    .replace(
      'headers: options.body ? { "Content-Type": "application/json" } : undefined,',
      'headers: Object.assign(options.body ? { "Content-Type": "application/json" } : {}, (window.localStorage.getItem("propai_social_flow_token") ? { Authorization: "Bearer " + window.localStorage.getItem("propai_social_flow_token") } : {})),',
    );
}

async function authorized(request: NextRequest) {
  const authorization = request.headers.get("authorization") || "";
  const token = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  if (!token) return false;
  const { data, error } = await getSupabaseAdmin().auth.getUser(token);
  return !error && Boolean(data.user);
}

async function proxy(request: NextRequest, path: string[]) {
  const isStudioAsset = path[0] === "studio";
  if (!isStudioAsset && !(await authorized(request))) {
    return NextResponse.json({ error: "Authentication required" }, { status: 401 });
  }
  if (!SDK_BASE) {
    return NextResponse.json(
      { error: "Realtor Ads Studio is not configured yet. Set SOCIAL_FLOW_SDK_URL on the app service." },
      { status: 503 },
    );
  }

  const sdkPath = isStudioAsset ? path.slice(1) : path;
  const target = `${SDK_BASE}/${sdkPath.join("/")}${new URL(request.url).search}`;
  const headers = new Headers();
  headers.set("Accept", "application/json");
  if (SDK_KEY) headers.set("X-Gateway-Key", SDK_KEY);
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);

  const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer();
  const response = await fetch(target, {
    method: request.method,
    headers,
    body,
    cache: "no-store",
  });

  const responseContentType = response.headers.get("content-type") || "application/octet-stream";
  if (isStudioAsset && sdkPath.at(-1) === "app.js" && responseContentType.includes("javascript")) {
    return new NextResponse(patchStudioScript(await response.text()), {
      status: response.status,
      headers: { "Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  return new NextResponse(response.body, {
    status: response.status,
    headers: {
      "Content-Type": responseContentType,
      "Cache-Control": "no-store",
    },
  });
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path || []);
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path || []);
}
