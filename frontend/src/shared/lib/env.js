// Phase 272/K8SFIX-03: prod-only flag baked at build time via
// VITE_ENVIRONMENT (set in deploy/k8s-images/frontend/Dockerfile for k8s,
// passed through from the root .env for compose). Vite statically replaces
// import.meta.env.VITE_* at build time — never a runtime read.
export const IS_PRODUCTION = import.meta.env.VITE_ENVIRONMENT === 'production'