/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the REST API, e.g. https://abc123.execute-api.eu-central-1.amazonaws.com/prod */
  readonly VITE_API_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
