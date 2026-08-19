const CACHE_NAME = "small-museum-static-v1";

const PRECACHE_URLS = [
    "/static/offline.html",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png"
];


/* =========================
   インストール
========================= */

self.addEventListener("install", event => {

    event.waitUntil(
        caches
            .open(CACHE_NAME)
            .then(cache => {
                return cache.addAll(PRECACHE_URLS);
            })
    );

    self.skipWaiting();
});


/* =========================
   古いキャッシュ削除
========================= */

self.addEventListener("activate", event => {

    event.waitUntil(
        caches
            .keys()
            .then(cacheNames => {
                return Promise.all(
                    cacheNames
                        .filter(name => name !== CACHE_NAME)
                        .map(name => caches.delete(name))
                );
            })
    );

    self.clients.claim();
});


/* =========================
   通信
========================= */

self.addEventListener("fetch", event => {

    const request = event.request;

    /*
     * POSTなどは触らない
     */
    if (request.method !== "GET") {
        return;
    }

    const url = new URL(request.url);

    /*
     * 外部サイトは触らない
     */
    if (url.origin !== self.location.origin) {
        return;
    }


    /* -------------------------
       HTMLページ

       展示室・作品は個人データなので
       キャッシュせずネット優先
    ------------------------- */

    if (request.mode === "navigate") {

        event.respondWith(
            fetch(request)
                .catch(() => {
                    return caches.match(
                        "/static/offline.html"
                    );
                })
        );

        return;
    }


    /* -------------------------
       CSS・JS・アイコンなど
    ------------------------- */

    if (url.pathname.startsWith("/static/")) {

        event.respondWith(
            caches.match(request)
                .then(cachedResponse => {

                    const networkResponse =
                        fetch(request)
                            .then(response => {

                                if (response.ok) {

                                    const copy =
                                        response.clone();

                                    caches
                                        .open(CACHE_NAME)
                                        .then(cache => {
                                            cache.put(
                                                request,
                                                copy
                                            );
                                        });
                                }

                                return response;
                            });

                    return (
                        cachedResponse ||
                        networkResponse
                    );
                })
        );
    }
});