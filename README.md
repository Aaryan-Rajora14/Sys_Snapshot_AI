# 🖥️ Sys_Snapshot_AI

> **An intelligent AI-powered system specification extractor for Windows PCs and Laptops**

Convert DxDiag reports into beautifully formatted, interactive HTML dashboards with complete hardware documentation—**all with one click**.

[![Python](https://img.shields.io/badge/Python-3.8%2B-brightgreen?style=flat-square&logo=python)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Code Size](https://img.shields.io/github/languages/code-size/Aaryan-Rajora14/Sys_Snapshot_AI?style=flat-square)](https://github.com/Aaryan-Rajora14/Sys_Snapshot_AI)

---

The Old Railway link was Unavailable.
Here's the new link - [syspec-snapshot-ai.up.railway.app](https://syspec-snapshot-ai.up.railway.app/)
This link is also available for 30 days i will try to provide new link every month.

## ✨ Key Features

### 🎯 Smart Hardware Detection
- **Intelligent GPU Recognition** — Automatically detects and categorizes Dedicated (NVIDIA/AMD) vs Integrated (Intel) GPUs
- **Comprehensive System Analysis** — Extracts CPU, RAM, storage, display, ports, and I/O specifications
- **Model-Specific Port Layouts** — Identifies and documents all available ports with precise configurations

### 📊 Beautiful Interactive Reports
- **5 Organized Dashboard Tabs**:
  - 📋 **Overview** — Quick system snapshot with key specs
  - 🎮 **GPU & Display** — Detailed graphics and monitor information
  - 🔌 **Ports & I/O** — Complete connectivity documentation
  - ⚡ **Performance** — Gaming FPS estimates with benchmark comparisons
  - 📝 **System Details** — Full raw data export and detailed specs

### 📄 Multiple Input/Output Formats
- **Input Support** — Process `.txt` and `.docx` DxDiag reports
- **Output Formats** — Generates professional HTML reports + Word + Text documents
- **Auto-Organization** — Reports automatically saved to `~/DxDiag_Outputs/`

### 🖥️ User-Friendly Interface
- **Web-Based Dashboard** — Local Flask web server with modern cyber-tech design
- **Drag & Drop Upload** — Simple file upload interface
- **One-Click Generation** — No complex configuration needed

---

## 🚀 Quick Start

### Prerequisites
- **Python 3.8 or higher**
- **Windows Operating System**
- **pip** (Python package manager)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/Aaryan-Rajora14/Sys_Snapshot_AI.git
   cd Sys_Snapshot_AI
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application**
   ```bash
   python laptop_extractor.py
   ```

4. **Open in your browser**
   - The application will automatically open at `http://localhost:5000`
   - Or manually navigate to the address shown in the terminal

### Usage

1. **Generate a DxDiag Report**
   - Press `Win + R`, type `dxdiag`, and press Enter
   - Click "Save All Information..." to export as `.txt` or `.docx`

2. **Upload to Sys_Snapshot_AI**
   - Drag and drop your DxDiag file into the web interface
   - Or click to browse and select the file

3. **View Your Report**
   - The AI automatically processes the DxDiag data
   - Interactive HTML dashboard opens with all hardware specifications
   - Reports are saved locally for future reference

---

## 📋 What Gets Extracted

### System Information
- Operating System & Build
- Processor (CPU) specifications and capabilities
- System RAM and memory configuration
- Storage drives and capacity

### Graphics & Display
- GPU Type (Dedicated/Integrated) and Model
- VRAM and Memory Interface
- Display Resolution and Refresh Rate
- Monitor specifications and capabilities

### Connectivity & Ports
- USB ports (2.0, 3.0, 3.1, Type-C)
- Audio ports (3.5mm, HDMI, DisplayPort, Thunderbolt)
- Network interfaces (Ethernet, Wi-Fi)
- Model-specific I/O configurations

### Performance Metrics
- Estimated Gaming FPS for popular titles
- Benchmark comparisons
- Performance rating and classification
- Thermal and power specifications

---

## 📁 Project Structure

```
Sys_Snapshot_AI/
├── laptop_extractor.py      # Main application logic & AI parser
├── requirements.txt         # Python dependencies
├── Procfile                 # Deployment configuration
├── README.md               # This file
└── DxDiag_Outputs/         # Auto-generated reports folder
```

---

## 🛠️ Technical Details

### Built With
- **Flask** — Web framework for the dashboard interface
- **Python-DOCX** — Word document processing
- **HTML/CSS** — Modern, responsive UI
- **AI-Powered Parsing** — Intelligent DxDiag data extraction and interpretation

### How It Works

1. **DxDiag Parsing** — Reads and parses DirectX diagnostic reports
2. **Data Extraction** — Uses intelligent algorithms to identify and categorize hardware components
3. **GPU Classification** — Smart detection distinguishes between GPU types and specifications
4. **Report Generation** — Creates comprehensive HTML, Word, and text documents
5. **Performance Analysis** — Estimates gaming capability and provides benchmark data

---

## 📊 Supported GPUs & Components

The tool intelligently recognizes:
- **NVIDIA** GPUs (GeForce, RTX, GTX series)
- **AMD** GPUs (Radeon, RDNA series)
- **Intel** Integrated Graphics (Iris, UHD, HD Graphics)
- **All major CPU brands** (Intel, AMD)
- **Standard RAM configurations** (DDR3, DDR4, DDR5)
- **All modern storage types** (SSD, NVMe, HDD)

---

## 🎯 Use Cases

Perfect for:
- 💼 **IT Technicians** — Quick hardware auditing and documentation
- 🎮 **Gamers** — Check gaming capability and performance estimates
- 🏪 **Hardware Resellers** — Generate professional specification sheets
- 📱 **Tech Enthusiasts** — Detailed system analysis and comparison
- 🔧 **System Administrators** — Hardware inventory and asset tracking

---

## 🔧 Troubleshooting

### DxDiag Report Not Found
Ensure you've properly exported the DxDiag information as a `.txt` or `.docx` file.

### Port Already in Use
If port 5000 is already in use, modify the port in `laptop_extractor.py` or close the conflicting application.

### Missing Dependencies
Reinstall requirements:
```bash
pip install -r requirements.txt
```

### Report Not Saving
Ensure the `DxDiag_Outputs` folder exists and has write permissions in your home directory.

---

## 💡 Tips & Best Practices

- **Export Recent DxDiag Reports** — Use the latest system diagnostics for accurate results
- **Use Word Format** — `.docx` files provide more complete information than `.txt`
- **Keep Reports Organized** — Reports are automatically saved; you can organize them by device
- **Check Benchmark Estimates** — Gaming FPS estimates are based on typical gaming workloads

---

## 🤝 Contributing

Contributions are welcome! Whether it's:
- 🐛 Bug reports and fixes
- ✨ Feature enhancements
- 📚 Documentation improvements
- 🎨 UI/UX improvements

Feel free to fork, modify, and submit pull requests!

---

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

---

## 👨‍💻 Author

**Aaryan Rajora** — [GitHub Profile](https://github.com/Aaryan-Rajora14)

---

## 🌟 Show Your Support

If you find Sys_Snapshot_AI helpful, please consider:
- ⭐ Starring this repository
- 🔄 Sharing with colleagues and friends
- 💬 Providing feedback and suggestions
- 🐛 Reporting issues and bugs

---

## 📞 Support & Feedback

Have questions or suggestions? 
- Open an [Issue](https://github.com/Aaryan-Rajora14/Sys_Snapshot_AI/issues)
- Check existing discussions
- Submit a pull request with improvements

---
Website Images: -

<img width="1901" height="878" alt="DxDiag Extractor v5 and 4 more pages - Personal - Microsoft​ Edge 31-05-2026 18_36_55" src="https://github.com/user-attachments/assets/9b7d1408-091d-4832-b1c0-27dfc17db4e9" />

<img width="1901" height="884" alt="DxDiag Extractor v5 and 4 more pages - Personal - Microsoft​ Edge 31-05-2026 18_47_47" src="https://github.com/user-attachments/assets/1e838a80-3e6c-4c3f-b0ff-38b47151f1b0" />

**Happy System Snapshotting! 🎉**
**Make Coding Great Again**
