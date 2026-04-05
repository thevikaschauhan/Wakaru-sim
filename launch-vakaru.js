#!/usr/bin/env node
// Launcher: changes to web-main dir then spawns Next.js
const { spawn } = require('child_process');
const path = require('path');

const webMainDir = '/Users/VikasChauhan/Desktop/vakaru-web/web-main';
const nextBin = path.join(webMainDir, 'node_modules/.bin/next');

process.chdir(webMainDir);

const port = process.env.PORT || '3000';

// Run next dev from the correct working directory
const child = spawn(
  '/usr/local/bin/node',
  [nextBin, 'dev', '--port', port],
  {
    cwd: webMainDir,
    stdio: 'inherit',
    env: { ...process.env }
  }
);

child.on('exit', (code) => process.exit(code));
process.on('SIGTERM', () => child.kill('SIGTERM'));
process.on('SIGINT', () => child.kill('SIGINT'));
