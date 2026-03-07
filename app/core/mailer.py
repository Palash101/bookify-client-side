from typing import List, Optional
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from app.core.settings import settings
import logging

logger = logging.getLogger(__name__)


class EmailService:
    """
    Email service for sending emails.
    """
    
    def __init__(self):
        self.conf = ConnectionConfig(
            MAIL_USERNAME=settings.SMTP_USER,
            MAIL_PASSWORD=settings.SMTP_PASSWORD,
            MAIL_FROM=settings.SMTP_FROM_EMAIL,
            MAIL_PORT=settings.SMTP_PORT,
            MAIL_SERVER=settings.SMTP_HOST,
            MAIL_STARTTLS=settings.SMTP_USE_TLS,
            MAIL_SSL_TLS=not settings.SMTP_USE_TLS,
            USE_CREDENTIALS=True,
            VALIDATE_CERTS=True
        )
        self.fastmail = FastMail(self.conf)
    
    async def send_email(
        self,
        subject: str,
        recipients: List[str],
        body: str,
        html_body: Optional[str] = None
    ) -> bool:
        """
        Send an email.
        
        Args:
            subject: Email subject
            recipients: List of recipient email addresses
            body: Plain text email body
            html_body: HTML email body (optional)
        
        Returns:
            bool: True if email sent successfully, False otherwise
        """
        try:
            message = MessageSchema(
                subject=subject,
                recipients=recipients,
                body=body,
                subtype="html" if html_body else "plain"
            )
            
            if html_body:
                message.body = html_body
            
            await self.fastmail.send_message(message)
            logger.info(f"Email sent successfully to {recipients}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {str(e)}")
            return False
    
    async def send_verification_email(self, email: str, token: str) -> bool:
        """
        Send email verification email.
        """
        subject = "Verify your email"
        html_body = f"""
        <html>
            <body>
                <h2>Email Verification</h2>
                <p>Please click the link below to verify your email:</p>
                <a href="http://localhost:3000/verify-email?token={token}">Verify Email</a>
            </body>
        </html>
        """
        return await self.send_email(subject, [email], "", html_body)
    
    async def send_password_reset_email(self, email: str, token: str) -> bool:
        """
        Send password reset email.
        """
        subject = "Reset your password"
        html_body = f"""
        <html>
            <body>
                <h2>Password Reset</h2>
                <p>Please click the link below to reset your password:</p>
                <a href="http://localhost:3000/reset-password?token={token}">Reset Password</a>
            </body>
        </html>
        """
        return await self.send_email(subject, [email], "", html_body)
    
    async def send_otp_email(self, email: str, otp_code: str, purpose: str = "verification") -> bool:
        """
        Send OTP email.
        
        Args:
            email: Recipient email address
            otp_code: OTP code to send
            purpose: Purpose of OTP ('login' or 'register')
        """
        purpose_text = "login" if purpose == "login" else "registration"
        subject = f"Your {purpose_text.title()} OTP - Bookify"
        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 30px;">
                    <h2 style="color: #333333; text-align: center;">Your OTP Code</h2>
                    <p style="color: #666666; font-size: 16px;">Hello,</p>
                    <p style="color: #666666; font-size: 16px;">Your OTP for {purpose_text} is:</p>
                    <div style="background-color: #f5f5f5; border-radius: 6px; padding: 20px; text-align: center; margin: 20px 0;">
                        <h1 style="color: #333333; font-size: 32px; letter-spacing: 5px; margin: 0;">{otp_code}</h1>
                    </div>
                    <p style="color: #666666; font-size: 14px;">This OTP will expire in 10 minutes.</p>
                    <p style="color: #999999; font-size: 12px; margin-top: 30px;">If you didn't request this OTP, please ignore this email.</p>
                    <p style="color: #999999; font-size: 12px;">Best regards,<br>Bookify Team</p>
                </div>
            </body>
        </html>
        """
        return await self.send_email(subject, [email], "", html_body)


email_service = EmailService()
