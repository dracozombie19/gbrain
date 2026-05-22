/**
 * src/core/cycle/git-export.ts — git_clone and git_push cycle phases.
 *
 * git_clone: runs before sync. Shallow-clones the configured remote repo to a
 * temp dir so that manual pushes the user made between cycles are picked up by
 * the sync phase.
 *
 * git_push: runs after purge. Exports all pages from the DB to the temp dir as
 * markdown, commits any delta, and pushes to the remote. Cleans up the temp dir
 * on completion (pass or fail).
 *
 * Auth: inject GITHUB_TOKEN into the HTTPS URL at clone/push time so no
 * credentials are written to disk. The token never appears in git config or
 * the commit log.
 *
 * Config keys (all under dream.git_export.*):
 *   repo_url        — HTTPS GitHub URL, e.g. https://github.com/user/brain.git
 *   branch          — branch to clone/push (default: main)
 *   commit_message  — supports {date} placeholder (default: gbrain export {date})
 */

import { execFile } from 'child_process';
import { promisify } from 'util';
import { mkdirSync, writeFileSync, rmSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { tmpdir } from 'os';
import type { BrainEngine } from '../engine.ts';
import type { PhaseResult, PhaseError } from '../cycle.ts';
import { serializeMarkdown } from '../markdown.ts';

const execFileAsync = promisify(execFile);

// ─── Config ────────────────────────────────────────────────────────

export interface GitExportConfig {
  repo_url: string;
  branch: string;
  commit_message: string;
}

/**
 * Resolution order for each key (matches the model-tier pattern):
 *   DB config (gbrain config set) → env var → built-in default
 *
 * Env vars are the reliable path for ephemeral containers where the DB starts
 * fresh. DB config is preferred so local overrides via `gbrain config set`
 * take effect without restarting the container.
 */
export async function loadGitExportConfig(engine: BrainEngine): Promise<GitExportConfig | null> {
  const url =
    (await engine.getConfig('dream.git_export.repo_url')) ??
    process.env.GBRAIN_GIT_EXPORT_REPO_URL ??
    null;
  if (!url) return null;

  const branch =
    (await engine.getConfig('dream.git_export.branch')) ??
    process.env.GBRAIN_GIT_EXPORT_BRANCH ??
    'main';

  const commit_message =
    (await engine.getConfig('dream.git_export.commit_message')) ??
    process.env.GBRAIN_GIT_EXPORT_COMMIT_MESSAGE ??
    'gbrain export {date}';

  return { repo_url: url, branch, commit_message };
}

/**
 * Inject GITHUB_TOKEN into an HTTPS URL as x-access-token credentials.
 * Returns the URL unchanged if GITHUB_TOKEN is not set or the URL isn't HTTPS.
 * The token is never written to disk or logged by git.
 */
function injectToken(repoUrl: string): string {
  const token = process.env.GITHUB_TOKEN;
  if (!token) return repoUrl;
  try {
    const url = new URL(repoUrl);
    if (!url.protocol.startsWith('https')) return repoUrl;
    url.username = 'x-access-token';
    url.password = token;
    return url.toString();
  } catch {
    return repoUrl;
  }
}

function makeError(e: unknown): PhaseError {
  const err = e instanceof Error ? e : new Error(String(e));
  const code = (err as NodeJS.ErrnoException).code ?? 'UNKNOWN';
  return {
    class: /ENOENT|EACCES|ENOTDIR/.test(code) ? 'FilesystemError' : 'InternalError',
    code,
    message: err.message.slice(0, 400),
  };
}

// ─── git_clone ─────────────────────────────────────────────────────

export interface GitCloneResult {
  result: PhaseResult & { phase: 'git_clone' };
  /** Absolute path to the cloned temp dir, or null if skipped/failed. */
  tempDir: string | null;
}

export async function runPhaseGitClone(engine: BrainEngine): Promise<GitCloneResult> {
  const skipped = (reason: string): GitCloneResult => ({
    result: {
      phase: 'git_clone',
      status: 'skipped',
      duration_ms: 0,
      summary: reason,
      details: { reason },
    },
    tempDir: null,
  });

  let config: GitExportConfig | null;
  try {
    config = await loadGitExportConfig(engine);
  } catch (e) {
    return skipped(`could not read git_export config: ${e instanceof Error ? e.message : String(e)}`);
  }

  if (!config) {
    return skipped('git_export not configured (gbrain config set dream.git_export.repo_url <url>)');
  }

  const tempDir = join(tmpdir(), `gbrain-export-${Date.now()}`);
  mkdirSync(tempDir, { recursive: true });

  try {
    const cloneUrl = injectToken(config.repo_url);
    await execFileAsync('git', [
      'clone',
      '--depth', '1',
      '--branch', config.branch,
      cloneUrl,
      tempDir,
    ]);

    return {
      result: {
        phase: 'git_clone',
        status: 'ok',
        duration_ms: 0,
        summary: `cloned ${config.repo_url} branch=${config.branch}`,
        details: { branch: config.branch, temp_dir: tempDir },
      },
      tempDir,
    };
  } catch (e) {
    // Clean up the temp dir we created before the clone failed.
    try { rmSync(tempDir, { recursive: true, force: true }); } catch { /* best-effort */ }
    return {
      result: {
        phase: 'git_clone',
        status: 'fail',
        duration_ms: 0,
        summary: 'git clone failed',
        details: { repo_url: config.repo_url, branch: config.branch },
        error: makeError(e),
      },
      tempDir: null,
    };
  }
}

// ─── git_push ──────────────────────────────────────────────────────

/**
 * Export all pages from the DB to tempDir as markdown files, then git add,
 * commit, and push. Skips the push when nothing changed. Cleans up tempDir
 * on completion regardless of outcome.
 */
export async function runPhaseGitPush(
  engine: BrainEngine,
  tempDir: string | null,
  dryRun: boolean,
): Promise<PhaseResult & { phase: 'git_push' }> {
  if (!tempDir || !existsSync(tempDir)) {
    return {
      phase: 'git_push',
      status: 'skipped',
      duration_ms: 0,
      summary: 'git_push skipped: no clone dir from git_clone phase',
      details: { reason: 'no_clone_dir' },
    };
  }

  let config: GitExportConfig | null = null;
  try {
    config = await loadGitExportConfig(engine);
  } catch { /* non-fatal — we can still use tempDir even if config re-read fails */ }

  try {
    // ── Export all pages from DB to the temp dir ──────────────────
    const refs = await engine.listAllPageRefs();
    let exported = 0;

    for (const { slug, source_id } of refs) {
      const page = await engine.getPage(slug, { sourceId: source_id });
      if (!page) continue;
      const tags = await engine.getTags(slug, { sourceId: source_id });

      const md = serializeMarkdown(
        (page.frontmatter ?? {}) as Record<string, unknown>,
        page.compiled_truth ?? '',
        page.timeline ?? '',
        { type: page.type ?? 'note', title: page.title ?? slug, tags },
      );

      // Mirror the disk layout from synthesize.ts: non-default sources go
      // under .sources/<id>/ so same-slug pages from different sources don't
      // overwrite each other.
      const filePath =
        source_id && source_id !== 'default'
          ? join(tempDir, '.sources', source_id, `${slug}.md`)
          : join(tempDir, `${slug}.md`);

      mkdirSync(dirname(filePath), { recursive: true });
      writeFileSync(filePath, md, 'utf-8');
      exported++;
    }

    if (dryRun) {
      return {
        phase: 'git_push',
        status: 'ok',
        duration_ms: 0,
        summary: `dry-run: would export ${exported} page(s) and push`,
        details: { exported, dry_run: true },
      };
    }

    // ── Check for changes ──────────────────────────────────────────
    const { stdout: statusOut } = await execFileAsync('git', [
      '-C', tempDir, 'status', '--porcelain',
    ]);

    if (!statusOut.trim()) {
      return {
        phase: 'git_push',
        status: 'ok',
        duration_ms: 0,
        summary: `exported ${exported} page(s) — no changes, push skipped`,
        details: { exported, changed: false },
      };
    }

    // ── Commit ─────────────────────────────────────────────────────
    const date = new Date().toISOString().slice(0, 10);
    const message = (config?.commit_message ?? 'gbrain export {date}').replace('{date}', date);

    await execFileAsync('git', ['-C', tempDir, 'add', '.']);
    await execFileAsync('git', [
      '-C', tempDir,
      '-c', 'user.name=gbrain',
      '-c', 'user.email=gbrain@localhost',
      'commit', '-m', message,
    ]);

    // ── Push ───────────────────────────────────────────────────────
    const branch = config?.branch ?? 'main';
    const pushUrl = config ? injectToken(config.repo_url) : 'origin';
    await execFileAsync('git', [
      '-C', tempDir,
      'push', pushUrl, `HEAD:${branch}`,
    ]);

    return {
      phase: 'git_push',
      status: 'ok',
      duration_ms: 0,
      summary: `exported ${exported} page(s) and pushed to remote (branch: ${branch})`,
      details: { exported, changed: true, branch },
    };
  } catch (e) {
    return {
      phase: 'git_push',
      status: 'fail',
      duration_ms: 0,
      summary: 'git_push phase failed',
      details: { temp_dir: tempDir },
      error: makeError(e),
    };
  } finally {
    // Always clean up the temp dir — even on failure — to avoid accumulating
    // gigabytes of cloned repos across failed cycles.
    try { rmSync(tempDir, { recursive: true, force: true }); } catch { /* best-effort */ }
  }
}
