// 用独立分支保存状态，避免 Actions 临时 runner 丢失前一天的歌单。
const fs = require('node:fs');
const branch = 'codex/song-history';
const path = 'song-history.json';

module.exports = async ({github, context, core, mode}) => {
  const repo = context.repo;
  if (mode === 'restore') {
    let firstRun = false;
    try {
      await github.rest.git.getRef({...repo, ref: `heads/${branch}`});
    } catch (error) {
      if (error.status !== 404) throw error;
      await github.rest.git.createRef({...repo, ref: `refs/heads/${branch}`, sha: context.sha});
      firstRun = true;
    }
    let data;
    if (firstRun) {
      // 在发送之前初始化远端文件，同时确认确实拥有状态写入权限。
      const created = await github.rest.repos.createOrUpdateFileContents({
        ...repo, path, branch,
        message: 'Initialize song recommendation history',
        content: Buffer.from('{"version":1,"entries":[]}\n').toString('base64'),
      });
      data = {
        sha: created.data.content.sha,
        content: Buffer.from('{"version":1,"entries":[]}\n').toString('base64'),
        type: 'file', encoding: 'base64',
      };
    } else {
      // 分支已存在时，缺失/损坏的历史必须报错，不能当成空歌单继续推送。
      ({data} = await github.rest.repos.getContent({...repo, path, ref: branch}));
    }
    if (data.type !== 'file' || data.encoding !== 'base64') {
      throw new Error('Song history must be a base64-encoded file');
    }
    fs.writeFileSync(path, Buffer.from(data.content, 'base64'));
    core.setOutput('sha', data.sha);
    return;
  }
  if (mode !== 'save') throw new Error(`Unknown history sync mode: ${mode}`);
  const sha = process.env.HISTORY_SHA;
  if (!sha) throw new Error('Missing restored history SHA');
  const {data} = await github.rest.repos.getContent({...repo, path, ref: branch});
  if (data.sha !== sha) throw new Error('Remote song history changed during this run');
  const content = fs.readFileSync(path);
  if (content.equals(Buffer.from(data.content, 'base64'))) return;
  await github.rest.repos.createOrUpdateFileContents({
    ...repo, path, branch, sha,
    message: `Update song recommendation history (run ${context.runId})`,
    content: content.toString('base64'),
  });
};
