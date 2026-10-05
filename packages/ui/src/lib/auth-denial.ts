/** An authoritative Response is required: Auth.js runs a custom middleware
 * handler after a boolean `authorized:false`; that handler can return next()
 * for a still-decodable JWT. A Response cannot be overridden by that branch. */
export function deniedAuthResponse(request:Request):Response {
  const current=new URL(request.url);
  if(current.pathname.startsWith('/api/'))return Response.json({error:'unauthorized'},{status:401});
  const signin=new URL('/signin',current.origin);
  signin.searchParams.set('callbackUrl',current.pathname+current.search);
  return Response.redirect(signin);
}
