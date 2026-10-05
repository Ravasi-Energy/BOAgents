// Next catch-all parameters are already decoded. Re-encode each router segment
// before constructing an upstream URL, so '?'/'#' cannot change route/query.
export function backendUrl(base:string,path:readonly string[],query:URLSearchParams):URL {
  const url=new URL(`${base.replace(/\/$/, '')}/${path.map(segment=>encodeURIComponent(segment)).join('/')}`);
  query.forEach((value,key)=>url.searchParams.append(key,value));
  return url;
}
