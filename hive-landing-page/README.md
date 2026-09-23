# Hive landing page

What T-Pot's own nginx serves at `https://<hive>:64297/` before you open any of the individual
tools: the dark "T-Pot" splash page with the particle background and the row of link boxes
(Attack Map, Kibana, Spiderfoot, ...). The deployer adds one link to the tools box:

```html
<a href="/sensors/" class="link" target="_blank">Sensor Deployer</a>
```

It's cosmetic: the deployer works without it. [`tpot-expansion-pack.sh`](../tpot-expansion-pack.sh)
applies it along with the nginx routes, so there's nothing to do by hand.

## Where the page lives

The page is baked into T-Pot's nginx image at `/var/lib/nginx/html/index.html`. The container is
read-only, nothing mounts the page from the host, and there is no `data/nginx/conf/index.html` in
a stock T-Pot install. Edits made inside the container are lost the next time T-Pot starts, which
recreates every container.

So the script:

1. copies the stock `index.html` out of the **installed** nginx image, so a newer T-Pot release's
   page (new tools, a new version number) is kept rather than overwritten by an old saved copy;
2. adds the link as the last entry of the tools box (the `<div class="link-box tools-box" ...>`),
   indented like the links above it;
3. writes the result to `$TPOT_DATA_PATH/nginx/conf/index.html`; and
4. bind-mounts it over the page in the image, in the `nginx` service of T-Pot's `docker-compose.yml`:

   ```yaml
   - ${TPOT_DATA_PATH}/nginx/conf/index.html:/var/lib/nginx/html/index.html:ro
   ```

nginx reads the page per request, so later changes to the file show up without a restart.

## After a T-Pot update or reinstall

A T-Pot update can replace `docker-compose.yml`, which removes the mount, and a fresh install or a
restore starts without the generated file. Either way the stock page comes back without the link.
Re-run `./tpot-expansion-pack.sh`. It rebuilds the page from the new stock copy and re-adds the
mount.

## Customizing it further

The script regenerates the page from stock on every run, so edits made directly to
`$TPOT_DATA_PATH/nginx/conf/index.html` are overwritten (the previous file is kept as
`index.html.bak-<timestamp>`). To make other changes, add them to the landing page step in
`tpot-expansion-pack.sh`.
