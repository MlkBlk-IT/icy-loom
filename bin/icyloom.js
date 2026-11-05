#!/usr/bin/env node
// IcyLoom npm CLI wrapper — delegates to Python package
// Installation via npm: npm install -g icyloom
// This wrapper runs the Python CLI (icyloom) if installed, otherwise provides instructions.

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const args = process.argv.slice(2);

// Try to find Python icyloom command
function findPython() {
  // Check for venv
  const venvPython = join(__dirname, '..', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  if (existsSync(venvPython)) return venvPython;
  
  // Check for python3 / python
  if (process.env.PYTHON) return process.env.PYTHON;
  
  if (process.platform === 'win32') {
    return 'py -3.12';
  }
  return 'python3';
}

function runIcyLoom(args) {
  const python = findPython();
  const isPyLauncher = python.includes('-3.12');
  
  let cmd, cmdArgs;
  if (isPyLauncher) {
    // py -3.12 -m icyloom.cli ...
    cmd = 'py';
    cmdArgs = ['-3.12', '-m', 'icyloom.cli', ...args];
  } else if (python.endsWith('python.exe') || python.endsWith('python') || python === 'python3') {
    cmd = python;
    cmdArgs = ['-m', 'icyloom.cli', ...args];
  } else {
    cmd = python;
    cmdArgs = ['-m', 'icyloom.cli', ...args];
  }
  
  const proc = spawn(cmd, cmdArgs, {
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });
  
  proc.on('exit', (code) => {
    process.exit(code);
  });
  
  proc.on('error', (err) => {
    if (err.code === 'ENOENT') {
      console.error('Python not found. IcyLoom requires Python 3.11+.');
      console.error('');
      console.error('Install Python from https://python.org, then run:');
      console.error('  pip install icyloom');
      console.error('');
      console.error('Or install via npm (this wrapper):');
      console.error('  npm install -g icyloom');
      process.exit(1);
    }
    console.error(err);
    process.exit(1);
  });
}

if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
  console.log(`
Usage: icyloom [command] [options]

IcyLoom — Unified local gateway: LLM cache + context drawer (MCP server)

Commands:
  init              Initialize configuration (~/.icyloom/config.yaml)
  doctor            Check installation and configuration
  serve             Start the gateway (ports 8150-8153)
  tray              Start the system tray icon
  check             Run full verification (21 checks)
  client            Configure MCP clients (Cursor, Claude Code, etc.)
  --version         Show version

Options:
  --help, -h        Show this help

Documentation: https://github.com/MlkBlk-IT/icy-loom
`);
  process.exit(0);
}

runIcyLoom(args);