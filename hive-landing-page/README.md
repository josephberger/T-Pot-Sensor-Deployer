# Hive landing page (optional)

What T-Pot's own nginx serves at `https://<hive>:64297/` before you log into any of the
individual tools - the dark "T-Pot" splash page with the particle background and the row of
link boxes (Attack Map, Kibana, Spiderfoot, ...).

[`index.html`](index.html) here is that page, T-Pot's stock template
(`docker/nginx/dist/html/index.html` in [telekom-security/tpotce](https://github.com/telekom-security/tpotce))
with exactly one line added to the tools box:

```diff
         <a href="/elasticvue/" class="link" target="_blank">Elasticvue</a>
         <a href="/kibana/" class="link" target="_blank">Kibana</a>
         <a href="/spiderfoot/" class="link" target="_blank">Spiderfoot</a>
+        <a href="/sensors/" class="link" target="_blank">Sensor Deployer</a>
     </div>
```

That's it - a link to this console, sitting next to T-Pot's other tools instead of being
something you have to remember the URL for.

## This is not part of the deployer app

It's not served by this project, not referenced by any of its code, and not required for the
deployer to work - `nginx/tpot-location.conf` (the routing block) is the only nginx change the
app actually needs. This is purely a cosmetic convenience edit to T-Pot's own landing page, kept
here so it isn't lost.

## Why it needs to live here at all

T-Pot's nginx container bind-mounts this file **read-only from the host**, from a path under
T-Pot's persistent data directory:

```yaml
# tpotce/docker-compose.yml, nginx service
- ${TPOT_DATA_PATH}/nginx/conf/index.html:/var/lib/nginx/html/index.html:ro
```

`install.sh` copies T-Pot's stock template into `data/nginx/conf/index.html` exactly once, the
first time T-Pot is installed. After that it's just a file on disk - nothing regenerates or
updates it again on its own, including a normal `update.sh`. That cuts both ways: your edit
survives updates and restarts, but a **fresh install** (new host, reinstalled T-Pot, restored
from a backup that predates the edit) lays down T-Pot's unmodified stock copy again and quietly
drops the Sensor Deployer link - with nothing to tell you it happened.

## Applying it

After T-Pot is installed and running:

```bash
cp hive-landing-page/index.html "$TPOT_HOST_DIR/data/nginx/conf/index.html"
```

No restart needed - nginx reads the file per request, so it just picks up the moment you
overwrite it.

If you've made other changes to T-Pot's landing page since this copy was taken, diff first
rather than overwriting blind:

```bash
diff "$TPOT_HOST_DIR/data/nginx/conf/index.html" hive-landing-page/index.html
```
