import { NextResponse } from "next/server";
import { getAgentProfile } from "@/lib/biomind-profile";
import { bioMindErrorBody } from "@/lib/biomind-session-adapter";

export const dynamic = "force-dynamic";
export async function GET() {
  try { return NextResponse.json(getAgentProfile(), { headers: { "Cache-Control": "no-store" } }); }
  catch (error) { return NextResponse.json(bioMindErrorBody(error), { status: 503 }); }
}
