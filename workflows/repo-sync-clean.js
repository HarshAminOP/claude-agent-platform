export const meta = {
  name: 'repo-sync-clean',
  description: 'Sync all repos with remote, clean stale branches, prune merged branches, and report status',
  whenToUse: 'Run periodically or when the user says /repo-sync-clean to keep workspace tidy',
  phases: [
    { title: 'Sync', detail: 'Fetch and pull all repos' },
    { title: 'Clean', detail: 'Prune stale and merged branches' },
    { title: 'Report', detail: 'Summary of workspace health' },
  ],
}

phase('Sync')

const syncResult = await agent(
  `Sync all git repositories in the current workspace. Execute these steps:

1. Find all git repos by running: find . -maxdepth 3 -name ".git" -type d | sed 's/\\/.git$//'
   (If no sub-repos found, check if current directory itself is a git repo)
2. For each repo found (run in batches to avoid overwhelming):
   - Run: git fetch --prune origin
   - If on main/master and working tree is clean: git pull --ff-only
   - If working tree is dirty: skip pull, note as "dirty"
   - If not on main/master: note current branch
3. Count: total repos, synced, dirty (skipped), on non-default branch

Important:
- Use SSH URLs only (repos already use SSH)
- Never force-pull or reset
- Skip repos with merge conflicts
- Report any fetch failures (network issues, auth problems)`,
  { label: 'sync:repos', phase: 'Sync', agentType: 'devops', schema: {
    type: 'object',
    properties: {
      totalRepos: { type: 'number' },
      synced: { type: 'number' },
      dirty: { type: 'array', items: { type: 'string' } },
      nonDefaultBranch: { type: 'array', items: { type: 'object', properties: {
        repo: { type: 'string' },
        branch: { type: 'string' }
      }}},
      fetchFailed: { type: 'array', items: { type: 'string' } },
      errors: { type: 'array', items: { type: 'string' } }
    }
  }}
)

log(`Sync: ${syncResult.synced}/${syncResult.totalRepos} repos synced, ${syncResult.dirty.length} dirty, ${syncResult.fetchFailed.length} failed`)

phase('Clean')

const cleanResult = await agent(
  `Clean up stale branches across all git repos in the current workspace. Find repos with: find . -maxdepth 3 -name ".git" -type d | sed 's/\\/.git$//'

For each repo:

1. List local branches: git branch
2. Identify branches that are SAFE to delete:
   - Branches fully merged into main/master (git branch --merged main)
   - Branches whose remote tracking branch is gone (git branch -vv | grep ': gone]')
   - EXCEPT: never delete main, master, develop, release/*, or the current branch
3. For branches safe to delete: run git branch -d <branch> (safe delete, won't force)
4. Run git gc --auto to clean up loose objects
5. Report what was cleaned

SAFETY RULES:
- Only use git branch -d (lowercase d) — NEVER -D (force delete)
- Never delete branches with unpushed commits (git branch -d will refuse, which is correct)
- Never delete branches the repo is currently checked out on
- Skip repos that are dirty (have uncommitted changes)
- If in doubt, DON'T delete`,
  { label: 'clean:branches', phase: 'Clean', agentType: 'devops', schema: {
    type: 'object',
    properties: {
      reposProcessed: { type: 'number' },
      branchesDeleted: { type: 'array', items: { type: 'object', properties: {
        repo: { type: 'string' },
        branch: { type: 'string' },
        reason: { type: 'string' }
      }}},
      branchesKept: { type: 'array', items: { type: 'object', properties: {
        repo: { type: 'string' },
        branch: { type: 'string' },
        reason: { type: 'string' }
      }}},
      gcRun: { type: 'number' },
      errors: { type: 'array', items: { type: 'string' } }
    }
  }}
)

log(`Clean: ${cleanResult.branchesDeleted.length} branches removed, ${cleanResult.branchesKept.length} kept (unmerged/active)`)

phase('Report')

const report = await agent(
  `Generate a workspace health report based on these sync and clean results:

SYNC RESULTS:
${JSON.stringify(syncResult, null, 2)}

CLEAN RESULTS:
${JSON.stringify(cleanResult, null, 2)}

Create a concise report covering:
1. Overall health (all synced? any problems?)
2. Repos needing attention (dirty, on feature branches, fetch failures)
3. Branches cleaned (how many, from which repos)
4. Recommendations (repos that should be switched back to main, dirty repos that need commits or stashes)

Also update the knowledge base at ~/.claude/knowledge/ if there are significant findings:
- If a repo has persistent issues, note it in the repo's knowledge file
- Update ~/.claude/knowledge/INDEX.md if needed`,
  { label: 'report:health', phase: 'Report', agentType: 'system', schema: {
    type: 'object',
    properties: {
      overallHealth: { type: 'string' },
      needsAttention: { type: 'array', items: { type: 'object', properties: {
        repo: { type: 'string' },
        issue: { type: 'string' },
        recommendation: { type: 'string' }
      }}},
      branchesCleaned: { type: 'number' },
      recommendations: { type: 'array', items: { type: 'string' } },
      knowledgeUpdated: { type: 'boolean' }
    }
  }}
)

return {
  sync: {
    total: syncResult.totalRepos,
    synced: syncResult.synced,
    dirty: syncResult.dirty.length,
    failed: syncResult.fetchFailed.length
  },
  clean: {
    branchesDeleted: cleanResult.branchesDeleted.length,
    branchesKept: cleanResult.branchesKept.length
  },
  health: report.overallHealth,
  needsAttention: report.needsAttention,
  recommendations: report.recommendations
}
