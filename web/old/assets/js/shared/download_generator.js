/**
 * Unified Download Script Generator
 * Based on the mature NIRCam implementation with support for both TOML and JSON manifests
 */

class DownloadGenerator {
    constructor(config = {}) {
        this.config = {
            baseUrl: config.baseUrl || 'https://data.hollisakins.com',
            showProgressBar: config.showProgressBar !== false,
            resumeSupport: config.resumeSupport !== false,
            createDirs: config.createDirs !== false,
            ...config
        };
    }

    /**
     * Generate download script from filtered file list
     * @param {Array} files - Array of file objects with {filename, path, size, sizeBytes, etc.}
     * @param {Object} metadata - Field name, total count, etc.
     * @returns {string} Bash script content
     */
    generateScript(files, metadata = {}) {
        if (!files || files.length === 0) {
            throw new Error('No files provided for download script generation');
        }

        const field = metadata.field || 'data';
        const totalSize = files.reduce((sum, file) => sum + (file.sizeBytes || 0), 0);
        const timestamp = new Date().toISOString();

        let script = this._generateHeader(field, files.length, totalSize, timestamp);
        script += this._generateSetup(field);
        script += this._generateFileDownloads(files);
        script += this._generateFooter();

        return script;
    }

    /**
     * Generate script header with metadata
     */
    _generateHeader(field, fileCount, totalSize, timestamp) {
        return `#!/bin/bash
# JWST Data Download Script
# Generated: ${timestamp}
# Field: ${field}
# Total files: ${fileCount}
# Total size: ${this.formatFileSize(totalSize)}

`;
    }

    /**
     * Generate setup section with directory creation and configuration
     */
    _generateSetup(field) {
        let setup = `# Create directory structure
mkdir -p ${field.toLowerCase()}_data
cd ${field.toLowerCase()}_data

# Download files with resume support and progress bar
# Using curl with:
#   -L: Follow redirects
#   -O: Save with original filename`;

        if (this.config.resumeSupport) {
            setup += `
#   -C -: Resume partial downloads`;
        }

        if (this.config.createDirs) {
            setup += `
#   --create-dirs: Create necessary directories`;
        }

        if (this.config.showProgressBar) {
            setup += `
#   --progress-bar: Show progress`;
        }

        setup += `

BASE_URL="${this.config.baseUrl}"

echo "Starting download of ${files.length} files..."
echo "Total size: ${this.formatFileSize(totalSize)}"
echo ""

`;
        return setup;
    }

    /**
     * Generate individual file download commands
     */
    _generateFileDownloads(files) {
        let downloads = '';

        files.forEach((file, index) => {
            const relativePath = file.path || file.filename;
            const cleanPath = relativePath.replace(/^\//, '');
            const directory = cleanPath.substring(0, cleanPath.lastIndexOf('/'));
            
            downloads += `# File ${index + 1}/${files.length}: ${file.filename}`;
            if (file.size) {
                downloads += ` (${file.size})`;
            }
            downloads += '\n';

            // Create directory if needed
            if (directory && directory !== cleanPath && this.config.createDirs) {
                downloads += `mkdir -p "${directory}"\n`;
            }

            downloads += `echo "Downloading ${file.filename}..."\n`;
            
            // Build curl command
            let curlCmd = 'curl -L';
            if (this.config.resumeSupport) curlCmd += ' -C -';
            if (this.config.createDirs) curlCmd += ' --create-dirs';
            if (this.config.showProgressBar) curlCmd += ' --progress-bar';
            
            curlCmd += ` -o "${cleanPath}" "$BASE_URL/${cleanPath}"\n`;
            downloads += curlCmd;

            if (index < files.length - 1) {
                downloads += 'echo ""\n\n';
            }
        });

        return downloads;
    }

    /**
     * Generate script footer
     */
    _generateFooter() {
        return `
echo ""
echo "Download complete!"
echo "Files saved in: $(pwd)"
`;
    }

    /**
     * Format file size in human-readable format
     * @param {number} bytes - Size in bytes
     * @returns {string} Formatted size string
     */
    formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    /**
     * Parse file size string to bytes
     * @param {string} sizeStr - Size string like "1.5 GB"
     * @returns {number} Size in bytes
     */
    parseFileSize(sizeStr) {
        if (!sizeStr) return 0;
        const size = parseFloat(sizeStr);
        const unit = sizeStr.toLowerCase();
        
        if (unit.includes('gb')) return size * 1024 * 1024 * 1024;
        if (unit.includes('mb')) return size * 1024 * 1024;
        if (unit.includes('kb')) return size * 1024;
        return size;
    }

    /**
     * Download script as file
     * @param {string} scriptContent - The generated script content
     * @param {string} filename - Optional custom filename
     */
    downloadAsFile(scriptContent, filename = null) {
        const defaultFilename = filename || `download_data_${Date.now()}.sh`;
        
        const blob = new Blob([scriptContent], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = defaultFilename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }

    /**
     * Copy script to clipboard
     * @param {string} scriptContent - The script content to copy
     * @returns {Promise<boolean>} Success status
     */
    async copyToClipboard(scriptContent) {
        try {
            await navigator.clipboard.writeText(scriptContent);
            return true;
        } catch (err) {
            console.error('Failed to copy script:', err);
            return false;
        }
    }
}

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = DownloadGenerator;
} else {
    window.DownloadGenerator = DownloadGenerator;
}