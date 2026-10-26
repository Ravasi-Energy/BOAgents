/** An authoritative Response is required: Auth.js runs a custom middleware
 * handler after a boolean `authorized:false`; that handler can return next()
 * for a still-decodable JWT. A Response cannot be overridden by that branch.
 * Both refusals can carry Auth.js cookies (csrf/callback), so no cache may keep them. */
const PRIVATE={'cache-control':'private, no-store'};
export function deniedAuthResponse(request:Request):Response {
  const current=new URL(request.url);
  if(current.pathname.startsWith('/api/'))return Response.json({error:'unauthorized'},{status:401,headers:PRIVATE});
  const signin=new URL('/signin',current.origin);
  signin.searchParams.set('callbackUrl',current.pathname+current.search);
  return new Response(null,{status:302,headers:{...PRIVATE,location:signin.toString()}});
}
