/* ==========================================================================
   Universal Downloader Pro - Client Script
   ========================================================================= */

document.addEventListener('DOMContentLoaded', () => {
    // --- DOM Elements ---
    const urlInput = document.getElementById('url-input');
    const pasteBtn = document.getElementById('paste-btn');
    const fetchBtn = document.getElementById('fetch-btn');
    const resultSection = document.getElementById('result-section');
    const mediaThumb = document.getElementById('media-thumb');
    const mediaDuration = document.getElementById('media-duration');
    const mediaTitle = document.getElementById('media-title');
    const platformBadge = document.getElementById('platform-badge');
    const platformText = document.getElementById('platform-text');
    const typeMp4 = document.getElementById('type-mp4');
    const typeMp3 = document.getElementById('type-mp3');
    const qualitySelect = document.getElementById('quality-select');
    const estSize = document.getElementById('est-size');
    const qualityContainer = document.getElementById('quality-selector-container');
    const downloadBtn = document.getElementById('download-btn');
    
    const progressSection = document.getElementById('progress-section');
    const progressStatus = document.getElementById('progress-status');
    const progressPercent = document.getElementById('progress-percent');
    const progressBarFill = document.getElementById('progress-bar-fill');
    const progressSpeed = document.getElementById('progress-speed');
    const progressEta = document.getElementById('progress-eta');
    
    const historyList = document.getElementById('history-list');
    const clearHistoryBtn = document.getElementById('clear-history-btn');
    const toastContainer = document.getElementById('toast-container');

    // --- State variables ---
    let activeType = 'mp4'; // Default download type
    let fetchedData = null; // Store metadata
    let pollInterval = null; // Store progress loop interval reference

    // --- Toast Notification Helper ---
    function showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        
        let iconClass = 'fa-solid fa-circle-info';
        if (type === 'success') iconClass = 'fa-solid fa-circle-check';
        if (type === 'error') iconClass = 'fa-solid fa-circle-exclamation';
        
        toast.innerHTML = `
            <i class="${iconClass} toast-icon"></i>
            <span class="toast-msg">${message}</span>
            <button class="toast-close"><i class="fa-solid fa-xmark"></i></button>
        `;
        
        toastContainer.appendChild(toast);
        
        // Remove toast on click of close button
        toast.querySelector('.toast-close').addEventListener('click', () => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => toast.remove(), 300);
        });
        
        // Auto-remove after 4 seconds
        setTimeout(() => {
            if (toast.parentNode) {
                toast.style.opacity = '0';
                toast.style.transform = 'translateX(100%)';
                setTimeout(() => toast.remove(), 300);
            }
        }, 4000);
    }

    // --- URL Validation Helper ---
    function isValidUrl(string) {
        try {
            const url = new URL(string);
            return url.protocol === 'http:' || url.protocol === 'https:';
        } catch (_) {
            return false;  
        }
    }

    // --- Clipboard Paste Handler ---
    pasteBtn.addEventListener('click', async () => {
        try {
            const text = await navigator.clipboard.readText();
            if (text) {
                urlInput.value = text.trim();
                showToast("Pasted from clipboard!", "success");
            } else {
                showToast("Clipboard is empty.", "info");
            }
        } catch (err) {
            showToast("Failed to read clipboard. Please paste manually.", "error");
        }
    });

    // Auto paste URL if valid on window focus
    window.addEventListener('focus', async () => {
        try {
            const text = await navigator.clipboard.readText();
            if (text && isValidUrl(text.trim()) && !urlInput.value.trim()) {
                urlInput.value = text.trim();
                showToast("Auto-pasted link from clipboard!", "success");
            }
        } catch (_) {
            // Silently fail if permissions aren't granted
        }
    });

    // Enter Key Trigger
    urlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            fetchBtn.click();
        }
    });

    // --- Fetch Video Metadata ---
    fetchBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        if (!url) {
            showToast("Please enter a valid URL.", "error");
            return;
        }
        if (!isValidUrl(url)) {
            showToast("The format of the link seems invalid.", "error");
            return;
        }

        // Set Loading state on Fetch Button
        fetchBtn.disabled = true;
        fetchBtn.innerHTML = `
            <i class="fa-solid fa-circle-notch spinner"></i>
            <span>Fetching Details...</span>
        `;
        
        // Clear previous configurations
        resultSection.classList.add('hidden');
        progressSection.classList.add('hidden');
        if (pollInterval) clearInterval(pollInterval);
        
        try {
            const response = await fetch('/formats', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: url })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || "Failed to retrieve formats.");
            }

            // Populate Metadata
            fetchedData = data;
            mediaTitle.textContent = data.title;
            mediaThumb.src = data.thumbnail;
            mediaDuration.textContent = data.duration;
            
            // Set Platform Badge Info
            platformBadge.className = `platform-badge ${data.platform}`;
            platformText.textContent = data.platform.charAt(0).toUpperCase() + data.platform.slice(1);
            
            // Set Platform Icon
            let platformIconClass = 'fa-solid fa-globe';
            if (data.platform === 'youtube') platformIconClass = 'fa-brands fa-youtube';
            else if (data.platform === 'instagram') platformIconClass = 'fa-brands fa-instagram';
            else if (data.platform === 'facebook') platformIconClass = 'fa-brands fa-facebook';
            else if (data.platform === 'tiktok') platformIconClass = 'fa-brands fa-tiktok';
            else if (data.platform === 'twitter') platformIconClass = 'fa-brands fa-x-twitter';
            else if (data.platform === 'vimeo') platformIconClass = 'fa-brands fa-vimeo-v';
            else if (data.platform === 'dailymotion') platformIconClass = 'fa-solid fa-play';
            
            platformBadge.querySelector('i').className = platformIconClass;

            // Populate quality dropdown options
            populateQualityDropdown(data.qualities);

            // Show result block and scroll to it
            resultSection.classList.remove('hidden');
            resultSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            showToast("Media details fetched successfully!", "success");

        } catch (err) {
            showToast(err.message, "error");
        } finally {
            // Reset Button state
            fetchBtn.disabled = false;
            fetchBtn.innerHTML = `
                <span>Fetch Media</span>
                <i class="fa-solid fa-arrow-right-long btn-arrow"></i>
            `;
        }
    });

    function populateQualityDropdown(qualities) {
        qualitySelect.innerHTML = '';
        if (!qualities || qualities.length === 0) {
            qualitySelect.innerHTML = `<option value="best" data-size="Unknown size">Best Quality</option>`;
            estSize.textContent = "Unknown size";
            return;
        }

        qualities.forEach(q => {
            const option = document.createElement('option');
            option.value = q.id;
            option.textContent = q.label;
            option.dataset.size = q.size;
            qualitySelect.appendChild(option);
        });

        // Set initial estimated size
        updateEstSize();
    }

    function updateEstSize() {
        if (activeType === 'mp3') {
            // Estimates size for 192kbps MP3 (roughly 1.4 MB per minute)
            if (fetchedData && fetchedData.duration !== "N/A") {
                const durationParts = fetchedData.duration.split(':');
                let totalSecs = 0;
                if (durationParts.length === 3) {
                    totalSecs = (+durationParts[0]) * 3600 + (+durationParts[1]) * 60 + (+durationParts[2]);
                } else if (durationParts.length === 2) {
                    totalSecs = (+durationParts[0]) * 60 + (+durationParts[1]);
                }
                const mbSize = (totalSecs * 192000) / (8 * 1024 * 1024);
                estSize.textContent = mbSize > 0 ? `${mbSize.toFixed(1)} MB` : "Unknown size";
            } else {
                estSize.textContent = "Unknown size";
            }
        } else {
            const selectedOption = qualitySelect.options[qualitySelect.selectedIndex];
            estSize.textContent = selectedOption ? selectedOption.dataset.size : "Unknown size";
        }
    }

    qualitySelect.addEventListener('change', updateEstSize);

    // --- MP4 / MP3 Toggle Handler ---
    typeMp4.addEventListener('click', () => {
        if (activeType === 'mp4') return;
        activeType = 'mp4';
        typeMp4.classList.add('active');
        typeMp3.classList.remove('active');
        qualityContainer.style.opacity = '1';
        qualityContainer.style.pointerEvents = 'auto';
        updateEstSize();
    });

    typeMp3.addEventListener('click', () => {
        if (activeType === 'mp3') return;
        activeType = 'mp3';
        typeMp3.classList.add('active');
        typeMp4.classList.remove('active');
        // Disable quality dropdown since MP3 uses fixed audio quality extraction
        qualityContainer.style.opacity = '0.5';
        qualityContainer.style.pointerEvents = 'none';
        updateEstSize();
    });

    // --- Download Video / Audio Task Trigger ---
    downloadBtn.addEventListener('click', async () => {
        if (!fetchedData) return;

        const url = urlInput.value.trim();
        const quality = activeType === 'mp4' ? qualitySelect.value : 'best';

        // Set Downloading state
        downloadBtn.disabled = true;
        downloadBtn.innerHTML = `
            <i class="fa-solid fa-circle-notch spinner"></i>
            <span>Queueing...</span>
        `;
        
        progressSection.classList.remove('hidden');
        progressStatus.textContent = "Initiating server download...";
        progressPercent.textContent = "0%";
        progressBarFill.style.width = "0%";
        progressSpeed.textContent = "0 KB/s";
        progressEta.textContent = "Unknown";
        
        progressSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

        try {
            const response = await fetch('/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    url: url,
                    quality: quality,
                    type: activeType
                })
            });

            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Failed to trigger download task.");
            }

            // Start polling progress
            startPollingProgress(data.task_id);

        } catch (err) {
            showToast(err.message, "error");
            resetDownloadBtn();
        }
    });

    function resetDownloadBtn() {
        downloadBtn.disabled = false;
        downloadBtn.innerHTML = `
            <i class="fa-solid fa-download download-icon-spin"></i>
            <span>Download Now</span>
        `;
    }

    // --- Progress Polling Loop ---
    function startPollingProgress(taskId) {
        if (pollInterval) clearInterval(pollInterval);
        
        downloadBtn.innerHTML = `
            <i class="fa-solid fa-circle-notch spinner"></i>
            <span>Downloading...</span>
        `;

        pollInterval = setInterval(async () => {
            try {
                const response = await fetch(`/progress/${taskId}`);
                if (!response.ok) {
                    throw new Error("Task expired or unavailable.");
                }

                const task = await response.json();

                if (task.status === 'downloading' || task.status === 'processing' || task.status === 'starting') {
                    // Update stats
                    let statusText = "Downloading stream...";
                    if (task.status === 'processing') statusText = "Merging audio & video files...";
                    if (task.status === 'starting') statusText = "Connecting to website...";
                    
                    progressStatus.textContent = statusText;
                    progressPercent.textContent = `${task.progress}%`;
                    progressBarFill.style.width = `${task.progress}%`;
                    progressSpeed.textContent = task.speed;
                    progressEta.textContent = task.eta;
                } 
                
                else if (task.status === 'completed') {
                    clearInterval(pollInterval);
                    progressStatus.textContent = "Download complete! Starting file delivery...";
                    progressPercent.textContent = "100%";
                    progressBarFill.style.width = "100%";
                    progressSpeed.textContent = "N/A";
                    progressEta.textContent = "0s";
                    
                    // Trigger download of the completed file
                    showToast("Download completed successfully!", "success");
                    window.location.href = `/download-file/${taskId}`;
                    
                    // Add entry to history list
                    saveToHistory(fetchedData.title, fetchedData.platform, activeType, qualitySelect.value, taskId);
                    
                    setTimeout(() => {
                        resetDownloadBtn();
                        progressSection.classList.add('hidden');
                    }, 4000);
                } 
                
                else if (task.status === 'failed') {
                    clearInterval(pollInterval);
                    throw new Error(task.error || "An error occurred during download.");
                }

            } catch (err) {
                clearInterval(pollInterval);
                showToast(err.message, "error");
                resetDownloadBtn();
                progressSection.classList.add('hidden');
            }
        }, 1000);
    }

    // --- Local Download History System ---
    function saveToHistory(title, platform, type, quality, taskId) {
        let history = JSON.parse(localStorage.getItem('downloader_history')) || [];
        
        // Build new history item
        const item = {
            id: taskId,
            title: title,
            platform: platform,
            type: type,
            quality: type === 'mp4' ? quality : 'Audio',
            date: new Date().toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
            url: urlInput.value.trim()
        };
        
        // Put newest first
        history.unshift(item);
        
        // Limit history count to 30 items
        if (history.length > 30) history.pop();
        
        localStorage.setItem('downloader_history', JSON.stringify(history));
        renderHistory();
    }

    function renderHistory() {
        let history = JSON.parse(localStorage.getItem('downloader_history')) || [];
        
        if (history.length === 0) {
            historyList.innerHTML = `
                <div class="empty-history">
                    <i class="fa-solid fa-cloud-arrow-down empty-icon"></i>
                    <p>Your download history is empty. Start fetching URLs!</p>
                </div>
            `;
            return;
        }

        historyList.innerHTML = '';
        history.forEach(item => {
            const historyItem = document.createElement('div');
            historyItem.className = 'history-item';
            
            // Format Platform icons for history list
            let platformIconClass = 'fa-solid fa-globe';
            if (item.platform === 'youtube') platformIconClass = 'fa-brands fa-youtube';
            else if (item.platform === 'instagram') platformIconClass = 'fa-brands fa-instagram';
            else if (item.platform === 'facebook') platformIconClass = 'fa-brands fa-facebook';
            else if (item.platform === 'tiktok') platformIconClass = 'fa-brands fa-tiktok';
            else if (item.platform === 'twitter') platformIconClass = 'fa-brands fa-x-twitter';
            else if (item.platform === 'vimeo') platformIconClass = 'fa-brands fa-vimeo-v';
            else if (item.platform === 'dailymotion') platformIconClass = 'fa-solid fa-play';

            historyItem.innerHTML = `
                <div class="history-item-left">
                    <span class="history-platform-icon ${item.platform}" title="${item.platform}">
                        <i class="${platformIconClass}"></i>
                    </span>
                    <div class="history-details">
                        <span class="history-title" title="${item.title}">${item.title}</span>
                        <div class="history-meta">
                            <span>${item.date}</span>
                            <span class="history-meta-badge">${item.type.toUpperCase()} • ${item.quality}</span>
                        </div>
                    </div>
                </div>
                <div class="history-item-right">
                    <button class="history-dl-btn redownload-action" data-url="${item.url}" title="Re-fetch & download">
                        <i class="fa-solid fa-redo"></i>
                    </button>
                    <button class="history-del-btn delete-action" data-id="${item.id}" title="Remove from list">
                        <i class="fa-regular fa-trash-can"></i>
                    </button>
                </div>
            `;
            
            // Attach individual action actions
            historyItem.querySelector('.redownload-action').addEventListener('click', (e) => {
                const targetUrl = e.currentTarget.dataset.url;
                urlInput.value = targetUrl;
                window.scrollTo({ top: 0, behavior: 'smooth' });
                fetchBtn.click();
                showToast("URL loaded from history!", "info");
            });

            historyItem.querySelector('.delete-action').addEventListener('click', (e) => {
                const targetId = e.currentTarget.dataset.id;
                deleteHistoryItem(targetId);
            });

            historyList.appendChild(historyItem);
        });
    }

    function deleteHistoryItem(id) {
        let history = JSON.parse(localStorage.getItem('downloader_history')) || [];
        history = history.filter(item => item.id !== id);
        localStorage.setItem('downloader_history', JSON.stringify(history));
        renderHistory();
        showToast("Download removed from history.", "info");
    }

    clearHistoryBtn.addEventListener('click', () => {
        let history = JSON.parse(localStorage.getItem('downloader_history')) || [];
        if (history.length === 0) return;
        
        if (confirm("Are you sure you want to clear your download history?")) {
            localStorage.removeItem('downloader_history');
            renderHistory();
            showToast("Download history cleared.", "success");
        }
    });

    // Initial render of history on page load
    renderHistory();

    // --- Premium Interactive Particles Canvas Effect ---
    const canvas = document.getElementById('particle-canvas');
    const ctx = canvas.getContext('2d');
    let particlesArray = [];

    // Resize listener
    window.addEventListener('resize', () => {
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
        initParticles();
    });

    class Particle {
        constructor(x, y, vx, vy, size, color) {
            this.x = x;
            this.y = y;
            this.vx = vx;
            this.vy = vy;
            this.size = size;
            this.color = color;
        }
        draw() {
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2, false);
            ctx.fillStyle = this.color;
            ctx.fill();
        }
        update() {
            if (this.x > canvas.width || this.x < 0) this.vx = -this.vx;
            if (this.y > canvas.height || this.y < 0) this.vy = -this.vy;
            this.x += this.vx;
            this.y += this.vy;
            this.draw();
        }
    }

    function initParticles() {
        particlesArray = [];
        let numberOfParticles = (canvas.width * canvas.height) / 10000;
        numberOfParticles = Math.min(numberOfParticles, 80); // Cap for performance stability
        
        for (let i = 0; i < numberOfParticles; i++) {
            let size = (Math.random() * 2) + 0.8;
            let x = (Math.random() * ((canvas.width - size * 2) - (size * 2)) + size * 2);
            let y = (Math.random() * ((canvas.height - size * 2) - (size * 2)) + size * 2);
            let vx = (Math.random() * 0.3) - 0.15;
            let vy = (Math.random() * 0.3) - 0.15;
            
            // Subtle theme matching red accents and gray base particles
            let color = Math.random() > 0.6 ? 'rgba(255, 26, 64, 0.22)' : 'rgba(255, 255, 255, 0.08)';
            particlesArray.push(new Particle(x, y, vx, vy, size, color));
        }
    }

    function connect() {
        for (let a = 0; a < particlesArray.length; a++) {
            for (let b = a; b < particlesArray.length; b++) {
                let dist = ((particlesArray[a].x - particlesArray[b].x) ** 2) + 
                           ((particlesArray[a].y - particlesArray[b].y) ** 2);
                
                // Connect particles if close enough
                if (dist < 15000) {
                    let alpha = 1 - (dist / 15000);
                    ctx.strokeStyle = `rgba(255, 26, 64, ${alpha * 0.06})`;
                    ctx.lineWidth = 0.8;
                    ctx.beginPath();
                    ctx.moveTo(particlesArray[a].x, particlesArray[a].y);
                    ctx.lineTo(particlesArray[b].x, particlesArray[b].y);
                    ctx.stroke();
                }
            }
        }
    }

    function animate() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        for (let i = 0; i < particlesArray.length; i++) {
            particlesArray[i].update();
        }
        connect();
        requestAnimationFrame(animate);
    }

    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    initParticles();
    animate();
});
