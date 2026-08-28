# Issue #103 Tier 1 evidence

Staging base: `https://78ffc78c25e0c8a9e64bb3a969ba6f226abae62d-8080.dstack-pha-prod7.phala.network`

Both cores answered health and version requests. Both versions report `159a02d`, and both
projects use image digest `sha256:efd53a4aeff1bbe0f05180c628a02b78661d955f56219e2e932800e8e13e1982`.

```text
GET /oauth3/_api/version
{"service":"oauth3-server","commit":"159a02d"}
GET /oauth3b/_api/version
{"service":"oauth3-server","commit":"159a02d"}
GET /oauth3/api/health
{"ready":true,"plugins":["otter","youtube","reddit","nytimes","twitter","google-calendar","amazon","zai","codex","hackernews"]}
GET /oauth3b/api/health
{"ready":true,"plugins":["otter","youtube","reddit","nytimes","twitter","google-calendar","amazon","hackernews"]}
POST /oauth3/api/login (subject A)
{"ok":true,"subject":"u-91762a5ffafca5d6ea0d8edd47cb2b88"}
POST /oauth3b/api/login (subject B)
{"ok":true,"subject":"u-2f36a96cc919ee77017396f8f4bb3243"}
GET /oauth3b/api/me with A session
{"signedIn":false,"providers":{"github":false,"google":false,"openkey":true},"links":[]}
GET /oauth3b/api/me with B session
{"signedIn":true,"subject":"u-2f36a96cc919ee77017396f8f4bb3243","providers":{"github":false,"google":false,"openkey":true},"links":[]}
```

The A session is rejected by B while B's independently-created session is accepted, proving
the session data is disjoint. The login responses shown above omit the bearer session values.
