document.addEventListener('DOMContentLoaded', function() {
    // DOM Elements
    const tabs = document.querySelectorAll('.tab');
    const tabContents = document.querySelectorAll('.tab-content');
    const uploadForm = document.getElementById('upload-form');
    const youtubeForm = document.getElementById('youtube-form');
    const videoFileInput = document.getElementById('video-file');
    const fileNameDisplay = document.getElementById('file-name');
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressInfo = document.getElementById('progress-info');
    const errorContainer = document.getElementById('error-container');
    const errorMessage = document.getElementById('error-message');
    const resultContainer = document.getElementById('result-container');
    const srtPreview = document.querySelector('.srt-preview');
    const downloadBtn = document.querySelector('.download-btn');
    
    let currentSrtContent = null;
    let currentFileName = null;
    
    // Add event listener for download button
    downloadBtn.addEventListener('click', function() {
        if (currentSrtContent) {
            downloadSrt(currentSrtContent, currentFileName || 'subtitles');
        } else {
            showNotification('لا توجد ترجمة متاحة للتنزيل', 'warning');
        }
    });
    
    // Show file name when selected
    videoFileInput.addEventListener('change', function() {
        if (this.files.length > 0) {
            const fileName = this.files[0].name;
            fileNameDisplay.textContent = fileName;
            fileNameDisplay.style.display = 'block';
            
            // Add animation to file name display
            fileNameDisplay.style.animation = 'none';
            setTimeout(() => {
                fileNameDisplay.style.animation = 'fadeIn 0.5s ease';
            }, 10);
        } else {
            fileNameDisplay.textContent = 'لم يتم اختيار ملف';
            fileNameDisplay.style.display = 'none';
        }
    });
    
    // Tab switching
    tabs.forEach(tab => {
        tab.addEventListener('click', function() {
            // Remove active class from all tabs and contents
            tabs.forEach(t => t.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));
            
            // Add active class to clicked tab and corresponding content
            this.classList.add('active');
            const tabId = this.getAttribute('data-tab');
            document.getElementById(tabId).classList.add('active');
        });
    });
    
    // Handle file upload form submission
    uploadForm.addEventListener('submit', function(e) {
        e.preventDefault();
        
        const fileInput = document.getElementById('video-file');
        const languageSelect = document.getElementById('language-select-file');
        
        if (!fileInput.files.length) {
            showNotification('الرجاء اختيار ملف فيديو', 'warning');
            return;
        }
        
        const file = fileInput.files[0];
        console.log('Uploading file:', file.name, 'Size:', file.size, 'Type:', file.type);
        
        if (file.size > 100 * 1024 * 1024) {  // 100MB limit
            showNotification('حجم الملف كبير جدًا. الحد الأقصى هو 100 ميجابايت.', 'warning');
            return;
        }
        
        // Check if file is a video
        if (!file.type.startsWith('video/')) {
            showNotification('الرجاء اختيار ملف فيديو صالح.', 'warning');
            return;
        }
        
        const formData = new FormData();
        formData.append('video', file);
        formData.append('language', languageSelect.value);
        
        // Show loading indicator and hide other containers
        resetUI();
        showProgress('جاري تحميل الفيديو...');
        
        // Send request
        fetch('/process', {
            method: 'POST',
            body: formData
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! Status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            console.log('Upload response:', data);
            
            if (data.task_id) {
                // Start checking progress
                currentFileName = file.name.replace(/\.[^/.]+$/, ""); // Remove extension
                checkProgress(data.task_id);
            } else if (data.subtitles || data.srt_content) {
                // Direct response with subtitles
                currentFileName = file.name.replace(/\.[^/.]+$/, ""); // Remove extension
                displayResult(data);
            } else {
                throw new Error('استجابة غير صالحة من الخادم');
            }
        })
        .catch(error => handleError(error, 'حدث خطأ أثناء تحميل الفيديو'));
    });
    
    // Handle YouTube form submission
    youtubeForm.addEventListener('submit', function(e) {
        e.preventDefault();
        
        const youtubeUrl = document.getElementById('youtube-url').value;
        const languageSelect = document.getElementById('language-select-youtube');
        
        if (!youtubeUrl) {
            showNotification('الرجاء إدخال رابط فيديو يوتيوب', 'warning');
            return;
        }
        
        // Basic YouTube URL validation
        if (!isValidYoutubeUrl(youtubeUrl)) {
            showNotification('الرجاء إدخال رابط يوتيوب صالح', 'warning');
            return;
        }
        
        // Show loading indicator and hide other containers
        resetUI();
        showProgress('جاري تحميل فيديو يوتيوب...');
        
        // Send request
        fetch('/process_youtube', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                youtube_url: youtubeUrl,
                language: languageSelect.value
            })
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! Status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            console.log('YouTube response:', data);
            
            if (data.task_id) {
                // Extract video ID or use a default name
                const videoId = extractYoutubeVideoId(youtubeUrl);
                currentFileName = videoId || 'youtube_video';
                
                // Start checking progress
                checkProgress(data.task_id);
            } else {
                throw new Error('استجابة غير صالحة من الخادم');
            }
        })
        .catch(error => handleError(error, 'حدث خطأ أثناء معالجة فيديو يوتيوب'));
    });
    
    // Function to display the result
    function displayResult(data) {
        const resultContainer = document.getElementById('result-container');
        const srtPreview = document.querySelector('.srt-preview');
        const loader = document.getElementById('loader');
        const progressContainer = document.getElementById('progress-container');
        
        console.log("Displaying result data:", data);
        
        // Hide loader and progress
        if (loader) loader.style.display = 'none';
        if (progressContainer) progressContainer.style.display = 'none';
        
        // Clear previous content
        srtPreview.innerHTML = '';
        
        // Check if there's an error
        if (data.error) {
            handleError(data.error);
            return;
        }
        
        // Check if we have subtitles
        if ((!data.subtitles || data.subtitles.trim() === '') && 
            (!data.srt_content || data.srt_content.trim() === '')) {
            handleError('لم يتم العثور على نص في الفيديو.');
            return;
        }
        
        // Get the subtitle content from either subtitles or srt_content
        const subtitleContent = data.subtitles || data.srt_content;
        
        // Format and display subtitles
        const formattedSubtitles = formatSubtitles(subtitleContent);
        srtPreview.innerHTML = formattedSubtitles;
        
        // Add download button functionality
        const downloadBtn = document.getElementById('download-btn');
        if (downloadBtn) {
            downloadBtn.onclick = function() {
                const fileName = data.filename || 'subtitles.srt';
                const srtContent = data.srt_content || subtitleContent;
                
                // Create a Blob with the subtitle content
                const blob = new Blob([srtContent], { type: 'text/plain' });
                
                // Create a download link
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = fileName;
                
                // Append to body, click and remove
                document.body.appendChild(a);
                a.click();
                
                // Clean up
                setTimeout(function() {
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                }, 0);
            };
        }
        
        // Set the current SRT content for download
        currentSrtContent = data.srt_content || subtitleContent;
        
        // Show result container
        resultContainer.style.display = 'block';
        
        // Show success notification
        showNotification('تم إنشاء الترجمة بنجاح!', 'success');
        
        // Scroll to result
        resultContainer.scrollIntoView({ behavior: 'smooth' });
        
        console.log("Result displayed successfully");
    }
    
    // Function to format subtitles
    function formatSubtitles(subtitles) {
        if (!subtitles) return '';
        
        // Fix any missing line breaks between subtitle blocks
        subtitles = subtitles.replace(/(\d+)(\s*)(\d{2}:\d{2}:\d{2},\d{3})/g, '$1\n$3');
        
        // Check if subtitles are in SRT format (1\n00:00:00,000 --> 00:00:05,000\nText)
        if (/\d+\s*\n\s*\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}/m.test(subtitles)) {
            console.log("Detected SRT format");
            
            // Split by double newlines or number at start of line
            const blocks = subtitles.split(/\n\s*\n|\n(?=\d+\s*\n)/);
            
            return blocks
                .map(block => {
                    // Clean up the block
                    block = block.trim();
                    if (!block) return '';
                    
                    // Split into lines
                    const lines = block.split(/\n/);
                    
                    // Find the timestamp line (contains "-->")
                    const timestampLineIndex = lines.findIndex(line => line.includes('-->'));
                    
                    if (timestampLineIndex >= 0) {
                        const timestamp = lines[timestampLineIndex].trim();
                        // Get all lines after the timestamp as text
                        const text = lines.slice(timestampLineIndex + 1).join('<br>');
                        
                        if (text.trim()) {
                            return `<div class="subtitle-block">
                                <div class="timestamp">${timestamp}</div>
                                <div class="text">${text}</div>
                                <button class="copy-text-btn" onclick="copySubtitleText(this)">
                                    <i class="fas fa-copy"></i> نسخ
                                </button>
                            </div>`;
                        }
                    }
                    
                    return '';
                })
                .filter(block => block) // Remove empty blocks
                .join('');
        }
        
        // Default formatting for plain text
        return subtitles.replace(/\n/g, '<br>');
    }
    
    // Function to copy subtitle text
    function copySubtitleText(button) {
        const subtitleBlock = button.closest('.subtitle-block');
        const textElement = subtitleBlock.querySelector('.text');
        const text = textElement.textContent || textElement.innerText;
        
        navigator.clipboard.writeText(text).then(() => {
            showNotification('تم نسخ النص بنجاح!', 'success');
            // Add visual feedback
            button.innerHTML = '<i class="fas fa-check"></i> تم النسخ';
            button.classList.add('copied');
            
            // Reset after a delay
            setTimeout(() => {
                button.innerHTML = '<i class="fas fa-copy"></i> نسخ';
                button.classList.remove('copied');
            }, 2000);
        }).catch(err => {
            console.error('Failed to copy text: ', err);
            showNotification('فشل نسخ النص!', 'error');
        });
    }
    
    // Function to check progress of a task
    function checkProgress(taskId) {
        console.log(`Checking progress for task: ${taskId}`);
        
        // Show progress container
        const progressContainer = document.getElementById('progress-container');
        if (progressContainer) progressContainer.style.display = 'block';
        
        // Reset progress bar
        showProgress();
        
        let checkInterval = 2000; // Start with 2 seconds
        let totalChecks = 0;
        let maxChecks = 300; // Maximum number of checks (10 minutes)
        let lastProgress = 0;
        let stuckCount = 0;
        
        const progressChecker = setInterval(function() {
            fetch(`/progress/${taskId}`)
                .then(response => {
                    if (!response.ok) {
                        throw new Error(`HTTP error! Status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    console.log('Progress data:', data);
                    
                    // Update progress bar and info
                    const progressPercent = data.progress || 0;
                    progressBar.style.width = `${progressPercent}%`;
                    progressBar.setAttribute('aria-valuenow', progressPercent);
                    
                    // Update progress percentage text
                    const progressPercentage = document.querySelector('.progress-percentage');
                    if (progressPercentage) {
                        progressPercentage.textContent = `${progressPercent}%`;
                    }
                    
                    if (data.message) {
                        progressInfo.textContent = data.message;
                    }
                    
                    // Check if progress is stuck
                    if (progressPercent === lastProgress) {
                        stuckCount++;
                        
                        // If progress is stuck for too long (30 seconds), show a message
                        if (stuckCount > 15 && progressPercent < 70) {
                            progressInfo.textContent = data.message + ' (قد تستغرق هذه العملية وقتًا طويلاً، يرجى الانتظار)';
                        }
                    } else {
                        stuckCount = 0;
                        lastProgress = progressPercent;
                    }
                    
                    // Check if task is complete
                    if (data.status === 'completed') {
                        clearInterval(progressChecker);
                        
                        if (data.result && (data.result.subtitles || data.result.srt_content)) {
                            displayResult(data.result);
                        } else if (data.subtitles || data.srt_content) {
                            displayResult(data);
                        } else {
                            handleError('لم يتم العثور على ترجمة في الفيديو');
                        }
                    } 
                    // Check if task failed
                    else if (data.status === 'failed' || data.error) {
                        clearInterval(progressChecker);
                        handleError(data.error || 'فشلت المهمة');
                    }
                    
                    // Increase check interval over time to reduce server load
                    totalChecks++;
                    if (totalChecks > 5) {
                        checkInterval = 3000; // 3 seconds after 5 checks
                    }
                    if (totalChecks > 10) {
                        checkInterval = 5000; // 5 seconds after 10 checks
                    }
                    
                    // Stop checking after maxChecks
                    if (totalChecks >= maxChecks) {
                        clearInterval(progressChecker);
                        handleError('انتهت مهلة المعالجة. يرجى المحاولة مرة أخرى.');
                    }
                })
                .catch(error => {
                    console.error('Error checking progress:', error);
                    // Don't clear interval on network errors, try again
                    totalChecks++;
                    
                    // If we've had too many errors in a row, stop checking
                    if (totalChecks >= 10) {
                        clearInterval(progressChecker);
                        handleError(error.message || 'حدث خطأ أثناء التحقق من التقدم');
                    }
                });
        }, checkInterval);
    }
    
    // Function to handle errors
    function handleError(errorMessage) {
        const resultContainer = document.getElementById('result-container');
        const resultContent = document.getElementById('result-content');
        
        // Create error element
        const errorElement = document.createElement('div');
        errorElement.className = 'error-message';
        errorElement.textContent = errorMessage;
        
        // Clear previous content and add error
        resultContent.innerHTML = '';
        resultContent.appendChild(errorElement);
        
        // Show result container
        resultContainer.style.display = 'block';
        
        // Add a try again button
        const tryAgainButton = document.createElement('button');
        tryAgainButton.textContent = 'حاول مرة أخرى';
        tryAgainButton.className = 'try-again-button';
        tryAgainButton.onclick = function() {
            // Reset the form
            document.getElementById('url-input').value = '';
            document.getElementById('file-input').value = '';
            document.getElementById('source-language').value = 'ar';
            document.getElementById('target-language').value = '';
            
            // Hide result container
            resultContainer.style.display = 'none';
            
            // Scroll to top
            window.scrollTo({ top: 0, behavior: 'smooth' });
        };
        
        resultContent.appendChild(tryAgainButton);
        
        // Scroll to result
        resultContainer.scrollIntoView({ behavior: 'smooth' });
        
        // Show notification
        showNotification('خطأ: ' + errorMessage, 'error');
    }
    
    // Function to add copy buttons to the subtitle container
    function addCopyButtons(container, data) {
        const copyButtonsDiv = document.createElement('div');
        copyButtonsDiv.className = 'copy-buttons';
        
        // Create copy original button
        const copyOriginalButton = document.createElement('button');
        copyOriginalButton.textContent = 'نسخ النص الأصلي';
        copyOriginalButton.className = 'copy-button';
        copyOriginalButton.onclick = function() {
            navigator.clipboard.writeText(data.subtitles)
                .then(() => showNotification('تم نسخ النص الأصلي!', 'success'))
                .catch(err => showNotification('فشل في نسخ النص: ' + err, 'error'));
        };
        copyButtonsDiv.appendChild(copyOriginalButton);
        
        // Create copy translated button if available
        if (data.translated_subtitles) {
            const copyTranslatedButton = document.createElement('button');
            copyTranslatedButton.textContent = 'نسخ النص المترجم';
            copyTranslatedButton.className = 'copy-button';
            copyTranslatedButton.onclick = function() {
                navigator.clipboard.writeText(data.translated_subtitles)
                    .then(() => showNotification('تم نسخ النص المترجم!', 'success'))
                    .catch(err => showNotification('فشل في نسخ النص: ' + err, 'error'));
            };
            copyButtonsDiv.appendChild(copyTranslatedButton);
        }
        
        // Create download as SRT button
        const downloadButton = document.createElement('button');
        downloadButton.textContent = 'تنزيل كملف SRT';
        downloadButton.className = 'download-button';
        downloadButton.onclick = function() {
            const filename = data.video_title ? 
                data.video_title.replace(/[^\w\s]/gi, '_') + '.srt' : 
                'subtitles.srt';
            
            // Convert to SRT format if needed
            let srtContent = data.subtitles;
            if (srtContent.includes('[') && srtContent.includes('-->')) {
                srtContent = convertToSrtFormat(srtContent);
            }
            
            downloadSrt(srtContent, filename);
        };
        copyButtonsDiv.appendChild(downloadButton);
        
        container.appendChild(copyButtonsDiv);
    }
    
    // Function to convert timestamp format to SRT format
    function convertToSrtFormat(subtitles) {
        let srtContent = '';
        let index = 1;
        
        const blocks = subtitles.split('\n\n');
        for (const block of blocks) {
            const lines = block.split('\n');
            if (lines.length >= 2 && lines[0].includes('-->')) {
                // Extract timestamp and convert format
                const timestamp = lines[0].replace('[', '').replace(']', '');
                const [start, end] = timestamp.split(' --> ');
                
                // Convert timestamp format from 00:00:00.000 to 00:00:00,000
                const formattedStart = start.replace('.', ',');
                const formattedEnd = end.replace('.', ',');
                
                // Get the text content
                const text = lines.slice(1).join('\n');
                
                // Add to SRT content
                srtContent += `${index}\n${formattedStart} --> ${formattedEnd}\n${text}\n\n`;
                index++;
            }
        }
        
        return srtContent.trim();
    }
    
    // Function to get language name from code
    function getLanguageName(languageCode) {
        const languages = {
            'ar': 'العربية',
            'en': 'الإنجليزية',
            'fr': 'الفرنسية',
            'es': 'الإسبانية',
            'de': 'الألمانية',
            'tr': 'التركية',
            'zh': 'الصينية',
            'ru': 'الروسية',
            'ja': 'اليابانية',
            'ko': 'الكورية',
            'it': 'الإيطالية',
            'pt': 'البرتغالية',
            'nl': 'الهولندية',
            'pl': 'البولندية',
            'hi': 'الهندية'
        };
        
        return languages[languageCode] || languageCode;
    }
    
    // Function to download SRT file
    function downloadSrt(content, filename = 'subtitles') {
        const blob = new Blob([content], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${filename}.srt`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }
    
    // Function to validate YouTube URL
    function isValidYoutubeUrl(url) {
        const youtubeRegex = /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.?be)\/.+$/;
        return youtubeRegex.test(url);
    }
    
    // Function to extract YouTube video ID
    function extractYoutubeVideoId(url) {
        const regExp = /^.*((youtu.be\/)|(v\/)|(\/u\/\w\/)|(embed\/)|(watch\?))\??v?=?([^#&?]*).*/;
        const match = url.match(regExp);
        return (match && match[7].length === 11) ? match[7] : null;
    }
    
    // Function to show progress
    function showProgress(message) {
        progressContainer.style.display = 'block';
        errorContainer.style.display = 'none';
        resultContainer.style.display = 'none';
        progressInfo.textContent = message || 'جاري المعالجة...';
        progressBar.style.width = '0%';
        document.querySelector('.progress-percentage').textContent = '0%';
    }
    
    // Function to reset UI
    function resetUI() {
        progressContainer.style.display = 'none';
        errorContainer.style.display = 'none';
        resultContainer.style.display = 'none';
        progressBar.style.width = '0%';
    }
    
    // Function to show notification
    function showNotification(message, type = 'info') {
        // Create notification element if it doesn't exist
        let notification = document.querySelector('.notification');
        if (!notification) {
            notification = document.createElement('div');
            notification.className = 'notification';
            document.body.appendChild(notification);
        }
        
        // Set message and type
        notification.textContent = message;
        notification.className = 'notification ' + type;
        
        // Show notification
        notification.style.display = 'block';
        notification.style.opacity = '1';
        
        // Hide after delay
        setTimeout(() => {
            notification.style.opacity = '0';
            setTimeout(() => {
                notification.style.display = 'none';
            }, 500); // transition duration
        }, 3000);
    }
    
    // Add notification styles
    const style = document.createElement('style');
    style.textContent = `
        .notification {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 15px 20px;
            background-color: white;
            color: #333;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
            display: flex;
            align-items: center;
            z-index: 1000;
            transform: translateX(120%);
            transition: transform 0.3s ease;
            max-width: 350px;
            direction: rtl;
        }
        
        .notification.show {
            transform: translateX(0);
        }
        
        .notification i {
            margin-left: 10px;
            font-size: 1.2rem;
        }
        
        .notification.info i {
            color: #3f51b5;
        }
        
        .notification.warning i {
            color: #ff9800;
        }
        
        .notification.error i {
            color: #f44336;
        }
        
        .notification.success i {
            color: #4caf50;
        }
        
        .notification .close-btn {
            background: none;
            border: none;
            color: #999;
            cursor: pointer;
            margin-right: 10px;
            padding: 5px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: auto;
            margin-left: 0;
        }
        
        .notification .close-btn:hover {
            color: #333;
        }
        
        .notification span {
            flex: 1;
        }
    `;
    document.head.appendChild(style);
    
    // Check for available features on load
    fetch('/setup_info')
        .then(response => response.json())
        .then(data => {
            console.log('Setup info:', data);
            
            // You can use this data to show/hide features based on availability
            if (!data.speech_recognition_available) {
                showNotification('تنبيه: ميزة التعرف على الكلام غير متوفرة. قد لا تعمل بعض الوظائف بشكل صحيح.', 'warning');
            }
        })
        .catch(error => {
            console.error('Error fetching setup info:', error);
        });
});

// Function to process YouTube URL
function processYouTubeURL() {
    // Get input values
    const youtubeURL = document.getElementById('youtube-url').value.trim();
    const sourceLanguage = document.getElementById('language-select-youtube').value;
    
    // Validate input
    if (!youtubeURL) {
        showNotification('يرجى إدخال رابط YouTube صالح', 'error');
        return;
    }
    
    // Basic YouTube URL validation
    if (!isValidYoutubeUrl(youtubeURL)) {
        showNotification('الرجاء إدخال رابط يوتيوب صالح', 'warning');
        return;
    }
    
    // Reset UI and show progress
    resetUI();
    showProgress('جاري تحميل فيديو يوتيوب...');
    
    console.log(`Processing YouTube URL: ${youtubeURL}`);
    console.log(`Source language: ${sourceLanguage}`);
    
    // Prepare data for API request
    const data = {
        youtube_url: youtubeURL,
        language: sourceLanguage
    };
    
    // Send request to server
    fetch('/process_youtube', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(data)
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! Status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        console.log("Received response from server:", data);
        
        // Extract video ID or use a default name
        const videoId = extractYoutubeVideoId(youtubeURL);
        currentFileName = videoId || 'youtube_video';
        
        // Check if this is a task ID for long-running process
        if (data.task_id) {
            checkProgress(data.task_id);
        } else if (data.subtitles || data.srt_content) {
            // Display result directly
            displayResult(data);
        } else {
            throw new Error('استجابة غير صالحة من الخادم');
        }
    })
    .catch(error => {
        console.error('Error processing YouTube URL:', error);
        handleError(error.message || 'حدث خطأ أثناء معالجة رابط YouTube');
    });
}
