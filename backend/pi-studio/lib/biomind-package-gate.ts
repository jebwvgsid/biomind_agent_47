/** Process-local admission gate complements the common runtime's cross-process leases. */
declare global { var __bioMindPackageMutation: boolean | undefined; }
export function packageManagementActive(): boolean { return globalThis.__bioMindPackageMutation === true; }
export function beginPackageManagement(): (() => void) | undefined {
  if (packageManagementActive()) return;
  globalThis.__bioMindPackageMutation = true;
  return () => { globalThis.__bioMindPackageMutation = false; };
}
