const paths: Record<string, string> = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  chat: '<path d="M21 11a8 8 0 0 1-8 8H7l-5 3 2-6a8 8 0 1 1 17-5Z"/>',
  search: '<circle cx="10" cy="10" r="6.5"/><path d="m15 15 5 5"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16M18 8v3"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  code: '<path d="m7 7-5 5 5 5m10-10 5 5-5 5M14 4l-4 16"/>',
  list: '<path d="m3 5 1 1 2-2m-3 8 1 1 2-2m-3 8 1 1 2-2M10 5h11M10 12h11M10 19h11"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
  terminal: '<rect x="2" y="4" width="20" height="16" rx="3"/><path d="m6 8 4 4-4 4m7 0h5"/>',
  arrow: '<path d="M12 20V4m-6 6 6-6 6 6"/>',
  link: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2m3 6a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2"/>',
  refresh: '<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
  wifi: '<path d="M3 8a15 15 0 0 1 18 0M6 12a10 10 0 0 1 12 0m-8 4a4 4 0 0 1 4 0M12 20h.01"/>',
  offline: '<path d="M3 3l18 18M3 8a15 15 0 0 1 3-1.5M10 5.5A15 15 0 0 1 21 8M6 12a10 10 0 0 1 5-2m5 1 2 1m-8 4a4 4 0 0 1 4 0M12 20h.01"/>',
  warning: '<path d="m10 4-8 14a2 2 0 0 0 2 3h16a2 2 0 0 0 2-3L14 4a2 2 0 0 0-4 0Z"/><path d="M12 9v4m0 4h.01"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2"/>',
  moon: '<path d="M20 14A9 9 0 0 1 10 3a9 9 0 1 0 10 11Z"/>',
  shield: '<path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6Z"/><path d="m8 12 3 3 5-6"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="3"/>',
  play: '<path d="m8 4 12 8-12 8Z"/>',
  leaf: '<path d="M20 3c-11-1-18 4-15 11 6 5 15 0 15-11ZM4 21 16 8"/>',
};
export function icon(name: string, size = 18): string {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.file}</svg>`;
}
/** Original BioMind mark, drawn for this prototype. */
export function logo(): string {
  return '<svg viewBox="0 0 32 32" width="30" height="30" aria-hidden="true"><rect width="32" height="32" rx="10" fill="#326951"/><path d="M10 24V9m0 7c0-7 12-9 12-5 0 3-8 4-12 5Zm0 0c5-1 14 0 13 5-1 5-10 4-13 3" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="22" cy="8" r="2" fill="#b9dc89"/></svg>';
}
