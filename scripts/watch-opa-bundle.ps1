# PP56: File watcher for OPA bundle hot-reload
# This script monitors ./policies directory for changes and rebuilds the bundle
# automatically when Rego files are modified.

param(
    [string]$PoliciesDir = "./policies",
    [string]$BundleDir = "./bundle",
    [string]$BundleName = "observability",
    [int]$PollInterval = 5
)

# Import required modules
Add-Type -AssemblyName System.IO

# Initialize variables
$lastBuildTime = [DateTime]::MinValue
$bundleFile = Join-Path $BundleDir "$BundleName.tar.gz"
$manifestFile = Join-Path $BundleDir "manifest.json"

Write-Host "Starting OPA bundle file watcher..."
Write-Host "Monitoring: $PoliciesDir"
Write-Host "Bundle output: $bundleFile"
Write-Host "Poll interval: $PollInterval seconds"

try {
    while ($true) {
        # Get the latest modification time in policies directory
        $latestFile = Get-ChildItem -Path $PoliciesDir -Recurse -File | 
                      Sort-Object LastWriteTime | 
                      Select-Object -Last 1
        
        if ($latestFile) {
            $latestModTime = $latestFile.LastWriteTime
            
            # Check if any file was modified since last build
            if ($latestModTime -gt $lastBuildTime) {
                Write-Host "[$(Get-Date)] Changes detected in policies, rebuilding bundle..."
                
                # Rebuild the bundle using the bash script
                $bashScript = Join-Path $PSScriptRoot "build-opa-bundle.sh"
                if (Test-Path $bashScript) {
                    # Use bash to run the build script (requires Git Bash or WSL)
                    $result = & bash $bashScript 2>&1
                    Write-Host $result
                    
                    # Update last build time
                    $lastBuildTime = [DateTime]::Now
                    Write-Host "Bundle rebuilt successfully at $($lastBuildTime)"
                } else {
                    Write-Error "Build script not found: $bashScript"
                }
            }
        }
        
        # Wait for next poll
        Start-Sleep -Seconds $PollInterval
    }
}
catch {
    Write-Error "File watcher error: $($_.Exception.Message)"
    exit 1
}