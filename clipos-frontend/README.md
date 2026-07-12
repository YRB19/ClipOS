# ClipOS Frontend

React + TypeScript frontend for ClipOS video clip generator.

## Setup

```bash
npm install
```

## Development

```bash
npm run dev
```

The dev server runs at `http://localhost:5173`.

## Environment Variables

Create a `.env` file:

```
VITE_API_BASE_URL=http://localhost:8000
```

## Backend Requirements

The automatic pipeline requires three processes running:

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: API server
cd clipos-backend
uvicorn app.main:app --reload

# Terminal 3: Worker
cd clipos-backend
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python worker.py
```

## Pages

- `/` — Job list (auto-refreshes)
- `/upload` — Upload file or YouTube URL
- `/jobs/:jobId/progress` — Pipeline progress with StatusRail
- `/jobs/:jobId` — Gallery of generated clips

## Tech Stack

- React 19 + TypeScript
- Vite
- Tailwind CSS v3 + shadcn/ui
- react-router-dom
- react-dropzone
- lucide-react icons
- date-fns

## Design Tokens

| Token | Color | Usage |
|-------|-------|-------|
| `ink` | `#14141B` | Page background |
| `ink-raised` | `#1B1B24` | Card background |
| `paper` | `#EDEAE2` | Primary text |
| `muted` | `#8B8A94` | Secondary text |
| `tally` | `#E8384F` | Primary accent (CTAs, active) |
| `tally-dim` | `#4A1B21` | Tally backgrounds/borders |
| `signal` | `#5EEAD4` | Data accent (scores, meters) |
| `signal-dim` | `#12332E` | Signal backgrounds/borders |
| `line` | `#2A2A35` | Borders, dividers |

## Fonts

- **Cabinet Grotesk** — Display/headings (500-600)
- **JetBrains Mono** — Numeric/timecode/status text
- **System sans** — Body/UI text
