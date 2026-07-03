# Shared VPN egress for pod.dstack apps

## Why
YouTube (and other sites) silently de-auth valid sessions replayed from the pod's
datacenter IP. Confirmed via `/api/youtube/debug`: complete modern cookies, full 715KB
page, `logged_in=0`, no consent wall. Fix = route app egress through a ProtonVPN exit.
Make it a shared, opt-in egress any project can use.

## Design
- **VPN project** `egress-vpn`: image `ghcr.io/amiller/openvpn-socks5`, `caps:["NET_ADMIN"]`
  (daemon grants `/dev/net/tun` only when attested), `egress_provider:true`. Exposes SOCKS5 :1080.
- **Daemon**: shared docker network `tee-egress`. Provider joins it as alias `egress-vpn`.
  A consumer project sets `egress:true` → daemon joins it to `tee-egress` + injects
  `EGRESS_PROXY_URL=socks5://egress-vpn:1080` (+ `ALL_PROXY`). No public ingress to the VPN.
- **App**: reads `EGRESS_PROXY_URL`, routes outbound via `Deno.createHttpClient({proxy})`.
  Verified: Deno 2.x supports socks5 proxy, no `--unstable` needed.

## Tasks
- [ ] daemon: `projects.py` add `egress`, `egress_provider` fields
- [ ] daemon: `docker_client.py` `connect_network` accepts aliases
- [ ] daemon: `runtimes.py` egress constants + attach-to-tee-egress + inject proxy env (start_image + start_isolated)
- [ ] daemon: `deploy.py` parse `egress`/`egress_provider` from manifest (both source + image paths)
- [ ] oauth3: youtube plugin routes fetch via `EGRESS_PROXY_URL` (+ debug probe); handler wires it
- [ ] cleanup: fix `jarToCookies` domain-flatten; remove `/api/youtube/debug` probe; revoke test token
- [ ] REHEARSE on webhost-staging CVM (daemon image rebuild) before pod.dstack
- [ ] deploy VPN project + flip oauth3 `egress:true`; verify feedling gets real history
