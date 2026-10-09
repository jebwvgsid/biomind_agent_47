/** A quiescence latch, not a task scheduler or an alternate resource lock. */
export class StudioPackageShutdown {
  private prepared = false;
  constructor(private locallyIdle: () => boolean, private domainIdle: () => Promise<boolean>) {}
  get ready(): boolean { return this.prepared; }
  async prepare(): Promise<void> {
    if (this.prepared) return;
    if (!this.locallyIdle()) throw new Error('PACKAGES_BUSY');
    if (!await this.domainIdle()) throw new Error('PACKAGES_BUSY');
    // Domain observation may await I/O; recheck the same owner afterward.
    if (!this.locallyIdle()) throw new Error('PACKAGES_BUSY');
    this.prepared = true;
  }
  cancel(): void { this.prepared = false; }
}
