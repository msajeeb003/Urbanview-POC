# frontend/ — UrbanView public map

Next.js 16 (App Router) + TypeScript + Tailwind v4 + shadcn/ui (Radix), Mapbox GL JS, TanStack
Query. Reproduces the client-approved wireframe exactly; rules, tokens, dimensions, layer list and
panel fields are in `CLAUDE.md` here, the design contract in `docs/specs/frontend-design.md`.

## Run

```bash
cp .env.example .env.local        # API base URL, Mapbox token (optional)
npm install                       # from the repository root (npm workspace)
npm run dev -w @urbanview/frontend
```

The API must be running (`make run` / `poe run` in `backend/`) and list `http://localhost:3000` in
`CORS_ORIGINS` (the default). Without `NEXT_PUBLIC_MAPBOX_TOKEN` the map area shows the wireframe
background; everything else works.

## Checks

```bash
npm run check -w @urbanview/frontend    # stylesheet copy + types + lint + unit tests
npm run build -w @urbanview/frontend
```

## API types

Generated from the backend's OpenAPI document, never written by hand:

```bash
cd backend && python -m api.export_openapi      # writes frontend/openapi.json
cd ../frontend && npm run api:types             # writes src/lib/api/schema.d.ts
```
