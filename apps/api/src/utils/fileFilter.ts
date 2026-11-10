export interface ChangedFile {
  filename: string;
  status?: string;
  patch?: string;
}

const IGNORE: RegExp[] = [
  // lockfiles
  /(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|Gemfile\.lock|composer\.lock|go\.sum)$/,
  // minified / sourcemaps / generated / snapshots
  /\.min\.(js|css)$/,
  /\.map$/,
  /\.generated\./,
  /\.snap$/,
  // build output and vendored folders (anywhere in the path)
  /(^|\/)(dist|build|node_modules|vendor|coverage|\.next)\//,
  // binary-ish assets
  /\.(png|jpe?g|gif|svg|ico|webp|pdf|woff2?|ttf|eot|zip|gz)$/i,
];

/** A file is reviewable if it has a text patch (binary / huge files have none) and is not ignored. */
export function isReviewable(file: ChangedFile, extraIgnore: RegExp[] = []): boolean {
  if (!file.patch) return false;
  if (file.status === "removed") return false;
  return ![...IGNORE, ...extraIgnore].some((r) => r.test(file.filename));
}

/** Lower number = reviewed first: source, then tests, then config, then docs. */
export function priority(filename: string): number {
  if (/\.(md|mdx|txt|rst)$/i.test(filename) || /(^|\/)docs?\//.test(filename)) return 3;
  if (/\.(json|ya?ml|toml|ini|env|lock)$/i.test(filename) || /(^|\/)\.[^/]+$/.test(filename)) return 2;
  if (/(\.|_)(test|spec)\.[a-z]+$/i.test(filename) || /(^|\/)(tests?|__tests__)\//.test(filename)) return 1;
  return 0;
}

export function selectFiles<T extends ChangedFile>(files: T[], extraIgnore: RegExp[] = []): T[] {
  return files
    .filter((f) => isReviewable(f, extraIgnore))
    .sort((a, b) => priority(a.filename) - priority(b.filename));
}
