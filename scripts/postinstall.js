// Postinstall script for npm package icyloom
// Checks for Python and the icyloom Python package, provides guidance if missing.

import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';

console.log('✓ IcyLoom npm wrapper installed');
console.log('');
console.log('IcyLoom is a Python application. To use it, ensure Python 3.11+ is installed:');
console.log('');
console.log('  # Install Python package:');
console.log('  pip install icyloom');
console.log('');
console.log('  # Or with pipx (recommended):');
console.log('  pipx install icyloom');
console.log('');
console.log('  # Then initialize:');
console.log('  icyloom init');
console.log('  icyloom doctor');
console.log('');
console.log('Documentation: https://github.com/MlkBlk-IT/icy-loom');
console.log('');

// Try to detect Python
try {
  const pythonVersion = execSync('python3 --version || python --version || py -3.12 --version', { 
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'ignore']
  }).trim();
  console.log(`✓ Found ${pythonVersion}`);
  
  // Check if icyloom is installed
  try {
    execSync('python3 -m icyloom --version || python -m icyloom --version || py -3.12 -m icyloom --version', {
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'ignore']
    });
    console.log('✓ IcyLoom Python package is installed');
    console.log('  Run: icyloom --help');
  } catch {
    console.log('⚠ IcyLoom Python package not found. Run: pip install icyloom');
  }
} catch {
  console.log('⚠ Python 3.11+ not found. Install from https://python.org');
}
