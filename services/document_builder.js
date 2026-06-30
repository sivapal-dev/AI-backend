const {
  Document,
  Packer,
  Paragraph,
  TextRun,
  Table,
  TableRow,
  TableCell,
  HeadingLevel,
  AlignmentType,
  BorderStyle,
  WidthType,
  PageBreak
} = require("docx");
const pptxgen = require("pptxgenjs");
const fs = require("fs");
const path = require("path");

// Theme Configurations for PPTX
const THEMES = {
  corporate: {
    bg: "0B1426",      // Navy title background
    textLight: "FFFFFF",
    textDark: "1E293B",
    primary: "1A3A6B",  // Blue
    secondary: "475569",
    accent: "00B4D8",   // Cyan accent
    slideBg: "F8FAFC"   // Very light blue-gray
  },
  modern: {
    bg: "1A1A2E",      // Dark charcoal
    textLight: "FFFFFF",
    textDark: "1F2937",
    primary: "0F3460",  // Dark blue accent
    secondary: "4B5563",
    accent: "E94560",   // Coral accent
    slideBg: "F9FAFB"
  },
  creative: {
    bg: "000000",      // Pure black
    textLight: "FFFFFF",
    textDark: "111827",
    primary: "007BFF",  // Electric blue
    secondary: "6B7280",
    accent: "00FF88",   // Neon green accent
    slideBg: "FFFFFF"
  }
};

/**
 * Helper to split text by bold (**) and italics (*) and return TextRun objects
 */
function parseTextInline(text, defaultOptions = {}) {
  if (!text) return [new TextRun({ text: "", ...defaultOptions })];
  const runs = [];
  // Split tokens by bold (**) and italics (*)
  const regex = /(\*\*.*?\*\*|\*.*?\*|[^*]+)/g;
  let match;
  while ((match = regex.exec(text)) !== null) {
    let part = match[0];
    let bold = defaultOptions.bold || false;
    let italics = defaultOptions.italics || false;
    
    if (part.startsWith('**') && part.endsWith('**')) {
      part = part.slice(2, -2);
      bold = true;
    } else if (part.startsWith('*') && part.endsWith('*')) {
      part = part.slice(1, -1);
      italics = true;
    }
    
    if (part) {
      runs.push(new TextRun({
        text: part,
        bold: bold,
        italics: italics,
        size: defaultOptions.size || 22, // 11pt default
        font: defaultOptions.font || "Calibri",
        color: defaultOptions.color
      }));
    }
  }
  return runs.length > 0 ? runs : [new TextRun({ text: "", ...defaultOptions })];
}

/**
 * Builds a beautiful DOCX document
 */
function buildDocx(data, outputPath) {
  const children = [];

  // 1. Cover Page
  const title = data.title || "Untitled Document";
  const subtitle = data.subtitle || "";
  const author = data.author || "By8flow AI";
  const date = data.date || new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });

  // Add cover page vertical space
  children.push(new Paragraph({
    spacing: { before: 2400 } // pushes title down
  }));

  // Title
  children.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [
      new TextRun({
        text: title,
        bold: true,
        size: 56, // 28pt
        font: "Calibri",
        color: "0B1426"
      })
    ],
    spacing: { after: 240 }
  }));

  // Subtitle
  if (subtitle) {
    children.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [
        new TextRun({
          text: subtitle,
          size: 32, // 16pt
          font: "Calibri",
          color: "475569"
        })
      ],
      spacing: { after: 1440 }
    }));
  }

  // Author & Date
  children.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [
      new TextRun({
        text: `${author}  |  ${date}`,
        size: 24, // 12pt
        font: "Calibri",
        color: "64748B"
      })
    ]
  }));

  // Page break to start body
  children.push(new Paragraph({
    children: [new PageBreak()]
  }));

  // 2. Sections
  const sections = data.sections || [];
  sections.forEach((sect, sectIdx) => {
    // Section Heading 1
    if (sect.title) {
      children.push(new Paragraph({
        heading: HeadingLevel.HEADING_1,
        children: [
          new TextRun({
            text: sect.title,
            bold: true,
            size: 40, // 20pt
            font: "Calibri",
            color: "0B1426"
          })
        ],
        spacing: { before: 360, after: 180 },
        keepWithNext: true
      }));
    }

    const content = sect.content || [];
    content.forEach((item) => {
      if (!item || !item.type) return;

      if (item.type === "paragraph") {
        children.push(new Paragraph({
          children: parseTextInline(item.text),
          spacing: { after: 160 },
          lineSpacing: 276 // 1.15 line spacing
        }));
      } 
      
      else if (item.type === "heading") {
        const level = item.level || 2;
        let headingLvl = HeadingLevel.HEADING_2;
        let fontSize = 32; // 16pt
        let color = "1A3A6B"; // Accent blue
        
        if (level === 1) {
          headingLvl = HeadingLevel.HEADING_1;
          fontSize = 40;
          color = "0B1426";
        } else if (level === 3) {
          headingLvl = HeadingLevel.HEADING_3;
          fontSize = 26; // 13pt
          color = "475569";
        }

        children.push(new Paragraph({
          heading: headingLvl,
          children: [
            new TextRun({
              text: item.text || "",
              bold: true,
              size: fontSize,
              font: "Calibri",
              color: color
            })
          ],
          spacing: { before: 240, after: 120 },
          keepWithNext: true
        }));
      } 
      
      else if (item.type === "list") {
        const items = item.items || [];
        const style = item.style || "bullet";
        items.forEach((listItem, idx) => {
          if (style === "number") {
            children.push(new Paragraph({
              children: [
                new TextRun({ text: `${idx + 1}.  `, bold: true, size: 22 }),
                ...parseTextInline(listItem)
              ],
              spacing: { after: 80 },
              indent: { left: 360 } // indent list item
            }));
          } else {
            children.push(new Paragraph({
              bullet: { level: 0 },
              children: parseTextInline(listItem),
              spacing: { after: 80 }
            }));
          }
        });
      } 
      
      else if (item.type === "code") {
        children.push(new Table({
          rows: [
            new TableRow({
              children: [
                new TableCell({
                  shading: { fill: "F1F5F9" },
                  borders: {
                    top: { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" },
                    bottom: { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" },
                    left: { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" },
                    right: { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" }
                  },
                  children: [
                    new Paragraph({
                      children: [
                        new TextRun({
                          text: item.text || "",
                          font: "Courier New",
                          size: 19 // 9.5pt
                        })
                      ],
                      spacing: { before: 100, after: 100 }
                    })
                  ]
                })
              ]
            })
          ],
          width: { size: 100, type: WidthType.PERCENTAGE }
        }));
        // spacing after table
        children.push(new Paragraph({ spacing: { after: 120 } }));
      } 
      
      else if (item.type === "table") {
        const headers = item.headers || [];
        const rows = item.rows || [];
        const tableRows = [];

        // Header Row
        if (headers.length > 0) {
          tableRows.push(new TableRow({
            children: headers.map(hdr => new TableCell({
              shading: { fill: "1A3A6B" }, // header blue
              borders: {
                top: { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" },
                bottom: { style: BorderStyle.SINGLE, size: 8, color: "1E293B" },
                left: { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" },
                right: { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" }
              },
              children: [
                new Paragraph({
                  children: [
                    new TextRun({ text: hdr || "", bold: true, color: "FFFFFF", size: 22 })
                  ],
                  alignment: AlignmentType.LEFT,
                  spacing: { before: 120, after: 120 }
                })
              ]
            }))
          }));
        }

        // Data Rows
        rows.forEach((row, rowIdx) => {
          if (!Array.isArray(row)) return;
          const bgFill = rowIdx % 2 === 0 ? "FFFFFF" : "F8FAFC"; // Zebra stripes
          tableRows.push(new TableRow({
            children: headers.map((_, colIdx) => {
              const cellText = row[colIdx] || "";
              return new TableCell({
                shading: { fill: bgFill },
                borders: {
                  top: { style: BorderStyle.SINGLE, size: 4, color: "E2E8F0" },
                  bottom: { style: BorderStyle.SINGLE, size: 4, color: "E2E8F0" },
                  left: { style: BorderStyle.SINGLE, size: 4, color: "E2E8F0" },
                  right: { style: BorderStyle.SINGLE, size: 4, color: "E2E8F0" }
                },
                children: [
                  new Paragraph({
                    children: parseTextInline(cellText),
                    spacing: { before: 100, after: 100 }
                  })
                ]
              });
            })
          }));
        });

        if (tableRows.length > 0) {
          children.push(new Table({
            rows: tableRows,
            width: { size: 100, type: WidthType.PERCENTAGE }
          }));
          // spacing after table
          children.push(new Paragraph({ spacing: { after: 160 } }));
        }
      }
    });

    // Page break between sections (except the last one)
    if (sectIdx < sections.length - 1) {
      children.push(new Paragraph({
        children: [new PageBreak()]
      }));
    }
  });

  const doc = new Document({
    sections: [{
      properties: {},
      children: children
    }]
  });

  return Packer.toBuffer(doc).then(buffer => {
    fs.writeFileSync(outputPath, buffer);
    console.log("DONE");
  });
}

/**
 * Builds a beautiful PPTX presentation
 */
function buildPptx(data, outputPath) {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";

  const themeName = data.theme || "corporate";
  const theme = THEMES[themeName] || THEMES.corporate;

  // 1. Title Slide
  const titleSlide = pres.addSlide();
  titleSlide.background = { color: theme.bg };
  
  titleSlide.addText(data.title || "Untitled Presentation", {
    x: 1.0,
    y: 2.0,
    w: "80%",
    h: 1.5,
    fontSize: 40,
    bold: true,
    color: theme.textLight,
    fontFace: "Arial"
  });

  if (data.subtitle) {
    titleSlide.addText(data.subtitle, {
      x: 1.0,
      y: 3.5,
      w: "80%",
      h: 1.0,
      fontSize: 20,
      color: theme.accent,
      fontFace: "Arial"
    });
  }

  const authorText = `${data.author || "By8flow AI"}  -  ${data.date || new Date().toLocaleDateString()}`;
  titleSlide.addText(authorText, {
    x: 1.0,
    y: 5.5,
    w: "80%",
    h: 0.5,
    fontSize: 12,
    color: "94A3B8",
    fontFace: "Arial"
  });

  // 2. Content Slides
  const slides = data.slides || [];
  slides.forEach((sld) => {
    const slide = pres.addSlide();
    slide.background = { color: theme.slideBg };

    const layout = sld.layout || "content";

    if (layout === "closing") {
      slide.background = { color: theme.bg };
      slide.addText(sld.title || "Thank You", {
        x: 1.0,
        y: 2.5,
        w: "80%",
        h: 1.5,
        fontSize: 44,
        bold: true,
        color: theme.textLight,
        align: "center",
        fontFace: "Arial"
      });
      if (sld.content && sld.content[0]) {
        slide.addText(sld.content[0], {
          x: 1.0,
          y: 4.0,
          w: "80%",
          h: 1.0,
          fontSize: 18,
          color: theme.accent,
          align: "center",
          fontFace: "Arial"
        });
      }
      return;
    }

    // Default top header bar
    slide.addShape(pres.ShapeType.rect, {
      x: 0,
      y: 0,
      w: "100%",
      h: 0.9,
      fill: { color: theme.primary }
    });

    slide.addText(sld.title || "Slide Title", {
      x: 0.5,
      y: 0.18,
      w: "90%",
      h: 0.5,
      fontSize: 24,
      bold: true,
      color: theme.textLight,
      fontFace: "Arial"
    });

    if (layout === "two_columns") {
      // Left Column
      const leftBullets = (sld.content_left || sld.content || []).map(bulletText => ({
        text: bulletText,
        options: { bullet: true, fontSize: 16, color: theme.textDark }
      }));
      slide.addText(leftBullets, {
        x: 0.5,
        y: 1.4,
        w: 4.2,
        h: 4.8,
        valign: "top",
        fontFace: "Arial"
      });

      // Right Column
      const rightBullets = (sld.content_right || []).map(bulletText => ({
        text: bulletText,
        options: { bullet: true, fontSize: 16, color: theme.textDark }
      }));
      slide.addText(rightBullets, {
        x: 5.2,
        y: 1.4,
        w: 4.2,
        h: 4.8,
        valign: "top",
        fontFace: "Arial"
      });
    } 
    
    else if (layout === "table" && sld.table) {
      const tableData = sld.table || {};
      const headers = tableData.headers || [];
      const rows = tableData.rows || [];
      
      const tableRows = [];
      if (headers.length > 0) {
        tableRows.push(headers.map(h => ({
          text: h,
          options: { bold: true, color: "FFFFFF", fill: theme.primary, align: "center" }
        })));
      }
      rows.forEach((row) => {
        tableRows.push(row.map(cell => ({
          text: cell || "",
          options: { color: theme.textDark, align: "left" }
        })));
      });

      if (tableRows.length > 0) {
        slide.addTable(tableRows, {
          x: 0.8,
          y: 1.4,
          w: 8.4,
          colW: Array(headers.length).fill(8.4 / headers.length)
        });
      }
    } 
    
    else {
      // Standard bullet point layout
      const bullets = (sld.content || []).map(bulletText => ({
        text: bulletText,
        options: { bullet: true, fontSize: 18, color: theme.textDark, lineSpacing: 24 }
      }));
      slide.addText(bullets, {
        x: 0.8,
        y: 1.4,
        w: 8.4,
        h: 4.8,
        valign: "top",
        fontFace: "Arial"
      });
    }
  });

  return pres.writeFile({ fileName: outputPath }).then(() => {
    console.log("DONE");
  });
}

/**
 * Main execution entry point
 */
function main() {
  const args = process.argv.slice(2);
  if (args.length < 3) {
    console.error("Usage: node document_builder.js <inputFile> <outputFile> <formatType>");
    process.exit(1);
  }

  const inputFile = args[0];
  const outputFile = args[1];
  const formatType = args[2];

  try {
    const rawContent = fs.readFileSync(inputFile, "utf8");
    const data = JSON.parse(rawContent);

    if (formatType === "doc") {
      buildDocx(data, outputFile)
        .then(() => process.exit(0))
        .catch(err => {
          console.error("DOCX Build Failure:", err);
          process.exit(1);
        });
    } else if (formatType === "ppt") {
      buildPptx(data, outputFile)
        .then(() => process.exit(0))
        .catch(err => {
          console.error("PPTX Build Failure:", err);
          process.exit(1);
        });
    } else {
      console.error("Unknown format type:", formatType);
      process.exit(1);
    }
  } catch (err) {
    console.error("Fatal exception in builder main:", err);
    process.exit(1);
  }
}

main();
