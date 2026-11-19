# Putting the shop behind the VM's web server

`caf serve` listens on port 8080 over plain HTTP. On the CX Nova VM the other apps are reached over HTTPS through
the machine's web server, so the shop should sit behind it too, either on its own host name or under a path prefix.

Two rules whatever the server:

1. **Prefix** — when the shop is served under a prefix (`https://host/agentshop/...`), set `CAF_ROOT_PATH=/agentshop`
   in `.env` and restart. Every page, the API docs, the console bridge and the storefront links then build their URLs
   under that prefix. The proxy must forward the request path *unchanged* (`/agentshop/shop` arrives as
   `/agentshop/shop`; the app strips the prefix itself).
2. **Forwarded headers** — the proxy sends `X-Forwarded-Proto` and `X-Forwarded-Host`, so links the API returns (the
   A2A card, short links) carry the public address. `caf serve` trusts them from `127.0.0.1` by default
   (`CAF_FORWARDED_ALLOW_IPS` widens it).

Set `CAF_PUBLIC_URL` to the public address as well (used for QR short links and shown by the service script).

## IIS with Application Request Routing (ARR)

Install *URL Rewrite* and *Application Request Routing*, enable the proxy in ARR (server node → Application
Request Routing Cache → Server Proxy Settings → Enable proxy, and turn off "Reverse rewrite host in response
headers"). Then in the site's `web.config`:

```xml
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="AgentShop" stopProcessing="true">
          <match url="^agentshop(/.*)?$" />
          <serverVariables>
            <set name="HTTP_X_FORWARDED_PROTO" value="https" />
            <set name="HTTP_X_FORWARDED_HOST" value="{HTTP_HOST}" />
          </serverVariables>
          <action type="Rewrite" url="http://127.0.0.1:8080/agentshop{R:1}" />
        </rule>
      </rules>
    </rewrite>
    <security>
      <requestFiltering><requestLimits maxAllowedContentLength="52428800" /></requestFiltering>
    </security>
  </system.webServer>
</configuration>
```

`HTTP_X_FORWARDED_PROTO` and `HTTP_X_FORWARDED_HOST` must be added to *Allowed Server Variables* in URL Rewrite.
Raise the proxy time-out in ARR (Server Proxy Settings → Time-out) to 600 seconds. Orchestrated runs answer within
`CAF_RUN_WAIT_S` (60 s by default) and are then followed with `GET /runs/{id}`, so the time-out only has to stay above
`CAF_RUN_WAIT_S` and the slowest single-agent call.

## nginx

```nginx
location /agentshop/ {
    proxy_pass http://127.0.0.1:8080/agentshop/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_read_timeout 600s;
    client_max_body_size 50m;
}
```

## Caddy

```
handle_path_prefix /agentshop/* {     # Caddy 2.9+: keeps the prefix on the upstream request
    reverse_proxy 127.0.0.1:8080 {
        transport http { read_timeout 600s }
    }
}
```

(On older Caddy use `handle /agentshop/* { reverse_proxy 127.0.0.1:8080 }`; Caddy sets the forwarded headers itself.)

## Own host name instead of a prefix

Point `agentshop.<your domain>` at the VM, proxy `/` to `http://127.0.0.1:8080/`, leave `CAF_ROOT_PATH` empty.

## No proxy at all

Open TCP 8080 in Windows Firewall (the service script does) and in the Azure network security group, and use
`http://<vm address>:8080/shop`. Fine for a first internal demo; not for customers, because the keys travel over
plain HTTP.
