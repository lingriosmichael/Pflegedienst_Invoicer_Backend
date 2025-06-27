import sys
import os
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QFileDialog,
    QLabel, QMessageBox, QLineEdit, QInputDialog, QStackedWidget
)
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import Qt, QSize
import app.pdf_parser
import app.database
import app.invoice_generator

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pflegedienst Jung Managementsystem")
        self.resize(400, 500)

        self.stack = QStackedWidget()
        self.main_menu = QWidget()
        self.invoice_view = InvoiceApp(self)
        self.analytics_view = AnalyticsWindow(self)

        self.init_main_menu()

        self.stack.addWidget(self.main_menu)
        self.stack.addWidget(self.invoice_view)
        self.stack.addWidget(self.analytics_view)

        layout = QVBoxLayout()
        layout.addWidget(self.stack)
        self.setLayout(layout)

    def init_main_menu(self):
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)  # control spacing between blocks

        self.main_menu.setStyleSheet("background-color: #3a3a3a;")

        # Block 1: Rechnungen für Privatleistungen
        invoice_block = QVBoxLayout()
        invoice_block.setAlignment(Qt.AlignCenter)
        invoice_block.setSpacing(2)

        invoice_btn = QPushButton()
        invoice_btn.setIcon(QIcon("icons/invoice.png"))
        invoice_btn.setIconSize(QSize(120, 120))
        invoice_btn.setFixedSize(140, 140)
        invoice_btn.setStyleSheet("""
            QPushButton {
                border-radius: 10px;
                background-color: rgba(255, 255, 255, 0.05);
                border: none;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.10);
            }
        """)
        invoice_btn.clicked.connect(self.open_invoice_view)
        invoice_block.addWidget(invoice_btn, alignment=Qt.AlignCenter)

        label_invoice = QLabel("Rechnungen für Privatleistungen")
        label_invoice.setAlignment(Qt.AlignCenter)
        label_invoice.setStyleSheet("font-size: 13px; color: #FFFFFF;")
        invoice_block.addWidget(label_invoice)

        # Block 2: Analytics
        analytics_block = QVBoxLayout()
        analytics_block.setAlignment(Qt.AlignCenter)
        analytics_block.setSpacing(2)

        analytics_btn = QPushButton()
        analytics_btn.setIcon(QIcon("icons/analytics.png"))
        analytics_btn.setIconSize(QSize(120, 120))
        analytics_btn.setFixedSize(140, 140)
        analytics_btn.setStyleSheet("""
            QPushButton {
                border-radius: 10px;
                background-color: rgba(255, 255, 255, 0.05);
                border: none;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.10);
            }
        """)
        analytics_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.analytics_view))
        analytics_block.addWidget(analytics_btn, alignment=Qt.AlignCenter)

        label_analytics = QLabel("Analytics")
        label_analytics.setAlignment(Qt.AlignCenter)
        label_analytics.setStyleSheet("font-size: 13px; color: #FFFFFF;")
        analytics_block.addWidget(label_analytics)

        # Add blocks to main layout
        layout.addLayout(invoice_block)
        layout.addLayout(analytics_block)

        self.main_menu.setLayout(layout)

    def open_invoice_view(self):
        if not self.invoice_view.abrechnungsmonat:
            self.invoice_view.prompt_abrechnungsmonat()

        self.stack.setCurrentWidget(self.invoice_view)

    def go_back(self):
        self.stack.setCurrentWidget(self.main_menu)

class InvoiceApp(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.setWindowTitle("Rechnungen für Privatleistungen")
        self.resize(500, 600)
        self.abrechnungsmonat = None

        self.layout = QVBoxLayout()

        # Section header
        self.header_label = QLabel("➡️ Rechnungen für Privatleistungen\nAbrechnungsmonat: Nicht gesetzt")
        self.header_label.setAlignment(Qt.AlignCenter)
        self.header_label.setStyleSheet("font-size: 18px; font-weight: bold; margin-bottom: 10px;")
        self.layout.addWidget(self.header_label)

        # Button to change Abrechnungsmonat
        change_month_btn = QPushButton("🗓️ Abrechnungsmonat ändern")
        change_month_btn.clicked.connect(self.prompt_abrechnungsmonat)
        self.layout.addWidget(change_month_btn)

        # Section: Import
        self.upload_btn = QPushButton("📄 Abrechnung importieren")
        self.upload_btn.clicked.connect(self.import_pdf)
        self.layout.addWidget(self.upload_btn)

        # Section: Rechnungen erstellen
        self.generate_btn = QPushButton("🖨️ Rechnungen erstellen")
        self.generate_btn.clicked.connect(self.generate_invoices)
        self.layout.addWidget(self.generate_btn)

        # Section: Tools label
        tools_label = QLabel("🛠️ Tools")
        tools_label.setAlignment(Qt.AlignCenter)
        tools_label.setStyleSheet("font-size: 16px; margin: 20px 0 10px 0; font-weight: bold;")
        self.layout.addWidget(tools_label)

        # Section: Tools buttons
        self.complete_btn = QPushButton("👤 Patientendaten ergänzen")
        self.complete_btn.clicked.connect(self.complete_data)
        self.layout.addWidget(self.complete_btn)

        self.check_service_btn = QPushButton("✅ Leistungsdaten überprüfen")
        self.check_service_btn.clicked.connect(self.check_service_fields)
        self.layout.addWidget(self.check_service_btn)

        self.retry_btn = QPushButton("🔄 Fehlerhafte Abrechnungen erneut verarbeiten")
        self.retry_btn.clicked.connect(self.retry_failed)
        self.layout.addWidget(self.retry_btn)

        self.regenerate_btn = QPushButton("🔄  Einzelne Rechnung erneut erstellen")
        self.regenerate_btn.clicked.connect(self.regenerate_invoice)
        self.layout.addWidget(self.regenerate_btn)

        # Spacer
        self.layout.addStretch()

        # Back button at bottom
        back_btn = QPushButton("🔙 Zurück zum Hauptmenü")
        back_btn.clicked.connect(self.parent.go_back)
        self.layout.addWidget(back_btn)

        # Apply modern button style
        self.set_button_style()

        self.setLayout(self.layout)
        app.database.init_db()

    def set_button_style(self):
        style = """
        QPushButton {
            border-radius: 8px;
            background-color: #444444;
            color: #FFFFFF;
            padding: 12px;
            font-size: 15px;
            border: 1px solid #555555;
        }

        QPushButton:hover {
            background-color: #555555;
        }
        """
        for button in [self.upload_btn, self.generate_btn, self.complete_btn,
                       self.check_service_btn, self.retry_btn, self.regenerate_btn]:
            button.setStyleSheet(style)

    def prompt_abrechnungsmonat(self):
        text, ok = QInputDialog.getText(self, "Abrechnungsmonat", "Abrechnungsmonat eingeben (z.B. 042025):")
        if ok and text.strip():
            self.abrechnungsmonat = text.strip()
            self.update_header_label()
            QMessageBox.information(self, "Gespeichert", f"Abrechnungsmonat gesetzt: {self.abrechnungsmonat}")
        else:
            QMessageBox.warning(self, "Fehlende Eingabe", "Abrechnungsmonat ist erforderlich.")

    def update_header_label(self):
        self.header_label.setText(f"➡️ Rechnungen für Privatleistungen\nAbrechnungsmonat: {self.abrechnungsmonat}")

    def import_pdf(self):
        file_name, ok = QInputDialog.getText(self, "PDF-Datei eingeben", "Name der PDF-Datei (z.B. may_2025_1.pdf):")
        if not ok or not file_name.strip():
            return

        path = f"data/abrechnung/{file_name.strip()}"

        if not os.path.exists(path):
            QMessageBox.warning(self, "Fehler", f"Datei nicht gefunden: {path}")
            return

        mode, ok = QInputDialog.getItem(self, "Leistungsart wählen", "Leistungsart:", ["sgbxi", "entleistung"], 0, False)
        if not ok:
            return

        text = app.pdf_parser.extract_text_from_pdf(path)
        chunks = app.pdf_parser.split_into_chunks(text)
        filtered = app.pdf_parser.filter_chunks_by_mode(chunks, mode)
        app.pdf_parser.process_import_sgbxi(filtered, self.abrechnungsmonat)
        QMessageBox.information(self, "Erfolg", f"{len(filtered)} Abschnitte importiert.")

    def generate_invoices(self):
        app.invoice_generator.process_generate_invoices(self.abrechnungsmonat)
        QMessageBox.information(self, "Fertig", "Rechnungen wurden erstellt.")

    def complete_data(self):
        app.database.check_missing_patient_fields(self.abrechnungsmonat)
        app.database.check_service_fields()
        QMessageBox.information(self, "Fertig", "Fehlende Patientendaten ergänzt und Leistungsdaten überprüft.")

    def check_service_fields(self):
        app.database.check_service_fields()
        QMessageBox.information(self, "Fertig", "Leistungsdaten wurden überprüft.")

    def retry_failed(self):
        app.pdf_parser.refeed_failed_chunk_from_file()
        QMessageBox.information(self, "Fertig", "Fehlerhafte Abrechnungen wurden erneut verarbeitet.")

    def regenerate_invoice(self):
        invoice_num, ok = QInputDialog.getText(self, "Rechnung erneut erstellen", "Rechnungsnummer eingeben:")
        if ok and invoice_num.strip():
            app.invoice_generator.regenerate_invoice(invoice_num.strip())
            QMessageBox.information(self, "Fertig", f"Rechnung {invoice_num.strip()} wurde erneut erstellt.")


class AnalyticsWindow(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.setWindowTitle("Analytics")
        self.resize(400, 300)

        layout = QVBoxLayout()

        label = QLabel("Analytics - Funktionen in Arbeit")
        layout.addWidget(label)

        # Back button
        back_btn = QPushButton("🔙 Zurück zum Hauptmenü")
        back_btn.clicked.connect(self.parent.go_back)
        layout.addWidget(back_btn)

        self.setLayout(layout)


if __name__ == "__main__":
    qt_app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(qt_app.exec_())
