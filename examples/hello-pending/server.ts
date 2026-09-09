export default async function handler(req: Request, ctx: { env: Record<string, string> }) {
  const url = new URL(req.url);
  if (url.pathname === "/") return new Response("hello from a self-provisioned project\n");
  if (url.pathname === "/whoami") return Response.json({ name: ctx.env.PROJECT_NAME ?? "hello-pending", pending: true });
  return new Response("not found", { status: 404 });
}
