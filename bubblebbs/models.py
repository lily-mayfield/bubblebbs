# FIXME: primary key being avoided because you have to do
# some annoying copypaste code to get primary keys to show
import copy
import os
import re
import pathlib
import datetime
from typing import Tuple, Union

import bleach
from flask import request
from bs4 import BeautifulSoup
from sqlalchemy.exc import (InvalidRequestError, IntegrityError)
from jinja2 import Markup
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename  # TODO: use for identicon

from . import config
from . import templating


db = SQLAlchemy()


class ErrorPageException(Exception):
    def __init__(self, format_docstring: dict = {}):
        self.message = self.__doc__.format(**format_docstring)
        super().__init__(self.message)
        self.http_status = self.HTTP_STATUS


class RemoteAddrIsBanned(ErrorPageException):
    """The remote address {address} attempted to perform an action
    it has been banned/prohibited from performing.

    Ban reason: {reason}

    """

    HTTP_STATUS = 420  # FIXME


class DuplicateMessage(ErrorPageException):
    """You tried to make a (duplicate) post that's already been
    made before. Please try to be more original.

    """

    HTTP_STATUS = 420  # FIXME


class TripMeta(db.Model):
    """Keeps track of tripcodes and postcount. Plus, if user
    proves they know unhashed version of tripcode they can
    set a Twitter URL, other links, a bio.

    """

    tripcode = db.Column(db.String(20), primary_key=True)
    post_count = db.Column(db.Integer, default=0, nullable=False)
    bio = db.Column(db.String(1000))
    bio_source = db.Column(db.String(400))

    @staticmethod
    def increase_post_count_or_create(tripcode: str):
        if tripcode:
            trip_meta = db.session.query(TripMeta).get(tripcode)
            if trip_meta:
                trip_meta.post_count += 1
            else:
                new_trip_meta = TripMeta(
                    tripcode=tripcode,
                    post_count=1,
                )
                db.session.add(new_trip_meta)
            db.session.commit()


class BannablePhrases(db.Model):
    phrase = db.Column(db.String(100), primary_key=True)

    @classmethod
    def check_for_bannable_phrases(cls, message: str):
        """

        Raises:
            RemoteAddrIsBanned: When `message` contains a banned
                phrase, the IP is banned, flagged, and this
                exception is raised with the details of the ban.

        """

        bannable_phrases = db.session.query(cls).all()
        for phrase in bannable_phrases:
            if phrase.phrase in message:
                FlaggedIps.new(request.remote_addr, 'bannable phrase')
                Ban.new(request.remote_addr, 'bannable phrase: ' + phrase.phrase)

                # TODO: weird use of an exception...
                raise RemoteAddrIsBanned(
                    format_docstring={
                        'address': request.remote_addr,
                        'reason': phrase.phrase,
                    },
                )



class FlaggedIps(db.Model):
    """Keeps track of which IPs have exhibited "bad behavior."

    `ip_address` is not unique so varieties
    of infractions can be recorded.

    """

    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(120), nullable=False)
    reason = db.Column(db.String(100))

    @classmethod
    def new(cls, ip_address_to_flag: str, flag_reason: str = None):
        db.session.add(cls(ip_address=ip_address_to_flag, reason=flag_reason))
        db.session.commit()
        db.session.flush()


# FIXME: bad schema...
# TODO: tags
class Post(db.Model):
    __tablename__ = 'posts'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120))
    ip_address = db.Column(db.String(120), nullable=False)
    locked = db.Column(db.Boolean(), default=False, nullable=False)
    verified = db.Column(db.Boolean(), default=False, nullable=False)
    permasage = db.Column(db.Boolean(), default=False, nullable=False)
    tripcode = db.Column(db.String(64))
    message = db.Column(db.String(2000), nullable=False, unique=True)
    reply_to = db.Column(db.Integer, db.ForeignKey('posts.id'))
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    bumptime = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    @staticmethod
    def name_tripcode_matches_original_use(name: str, tripcode: str) -> bool:
        """Verify that this usage of `name` has the correct
        tripcode as when `name` was originally used.

        """

        first_post_using_name = (
            Post.query
            .filter(Post.name == name)
            .order_by(Post.bumptime.asc())
            .first()
        )
        return (not first_post_using_name) or first_post_using_name.tripcode == tripcode

    @staticmethod
    def set_bump(form, reply_to, timestamp):
        if reply_to and not form.sage.data:
            original = db.session.query(Post).get(reply_to)
            if not original.permasage:
                original.bumptime = timestamp
                db.session.commit()

    @classmethod
    def from_form(cls, form):
        """Create and return a Post.

        The form may be a reply or a new post.

        Returns:
            Post: ...

        """

        # First the things which woudl prevent the post from being made
        Ban.ban_check(request.remote_addr)
        BannablePhrases.check_for_bannable_phrases(form.message.data)
        BannablePhrases.check_for_bannable_phrases(form.name.data)

        reply_to = int(form.reply_to.data) if form.reply_to.data else None
        if reply_to and db.session.query(Post).get(reply_to).locked:
            raise Exception('This thread is locked. You cannot reply.')

        # FIXME: should sanitize first?
        # Prepare info for saving to DB
        name, tripcode = templating.make_tripcode(form.name.data)
        if all([name, tripcode]):
            identicon = templating.ensure_identicon(tripcode)
            matches_original_use = cls.name_tripcode_matches_original_use(name, tripcode)
            verified = matches_original_use
            if not verified:
                FlaggedIps.new(request.remote_addr, 'unoriginal usage of name (considering tripcode)')
        else:
            verified = False

        timestamp = datetime.datetime.utcnow()

        # Save!
        new_post = cls(
            name=name,
            tripcode=tripcode,
            timestamp=timestamp,
            message=form.message.data,
            verified=verified,
            reply_to=reply_to,
            ip_address=request.remote_addr,
        )
        # NOTE: this block with the flush and rollback seems like
        # high potential for breaking everything when high traffic?
        try:
            db.session.add(new_post)
            db.session.commit()
            db.session.flush()
        except (InvalidRequestError, IntegrityError) as e:
            db.session.rollback()
            FlaggedIps.new(
                ip_address_to_flag=request.remote_addr,
                flag_reason='Duplicate post!',
            )
            raise DuplicateMessage()

        # TODO: after save method?
        TripMeta.increase_post_count_or_create(tripcode)
        cls.set_bump(form, reply_to, timestamp)

        return new_post


class Page(db.Model):
    __tablename__ = 'pages'
    slug = db.Column(db.String(60), primary_key=True)
    title = db.Column(db.String(200))
    body = db.Column(db.String(1000))
    source = db.Column(db.String(700))

    @classmethod
    def from_form(cls, form):
        body = templating.parse_markdown('lol', form.source.data)
        return cls(body=body, slug=form.slug.data, source=form.body.data)


# Create user model.
# TODO: rename admin?
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    login = db.Column(db.String(80), unique=True)
    email = db.Column(db.String(120))
    password = db.Column(db.String(200))

    # Flask-Login integration
    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id
    # Required for administrative interface
    def __unicode__(self):
        return self.username


class Ban(db.Model):
    """Admin can ban by address or network."""
    address = db.Column(db.String(100), primary_key=True)
    reason = db.Column(db.String(100))

    @classmethod
    def ban_check(cls, ip_address: str):
        ban = db.session.query(cls).get(ip_address)
        if ban:
            raise RemoteAddrIsBanned(format_docstring={'address': ban.address, 'reason': ban.reason})

    @classmethod
    def from_form(cls, form):
        new_ban = cls(
            address=form.address.data,
            reason=form.reason.data,
        )
        db.session.add(new_ban)
        db.session.commit()

        return new_ban

    @classmethod
    def new(cls, ip_address_to_ban: str, ban_reason: str = None) -> bool:
        try:
            db.session.add(cls(address=ip_address_to_ban, reason=ban_reason))
            db.session.commit()
            db.session.flush()
            return True
        except IntegrityError:
            db.session.rollback()
            return False


class BlotterEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(250))
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)


class ConfigPair(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(1000), nullable=False)


class WordFilter(db.Model):
    find = db.Column(db.String(100), primary_key=True)
    replace = db.Column(db.String(1000), nullable=False)  # can be html

    @classmethod
    def replace_all(cls, text_to_filter: str) -> Tuple[str, bool]:
        """Perform find/replace for every entry in word filter table.

        Another use is flagging users who have triggered the word filter.

        Arguments:
            text_to_filter: This text will have word filter performed
                on it. The result of the find/replace operations will
                be returned.

        Returns:
            filtered message, message was filtered (true/false): A two
                item tuple, the first element is the message with the
                word filter operations applied, the second is if the
                message has changed at all from such operations.

        """

        text_before_filtering = text_to_filter

        for word_filter in cls.get_all():
            # FIXME: candy-ies?
            find = re.compile(r'\b' + re.escape(word_filter.find) + r'(ies\b|s\b|\b)', re.IGNORECASE)
            # NOTE: I make it upper because I think it's funnier this way,
            # plus indicative of wordfiltering happening.
            text_to_filter = find.sub(word_filter.replace.upper(), text_to_filter)

        return (text_to_filter, text_before_filtering != text_to_filter)

    @classmethod
    def get_all(cls):
        return db.session.query(cls).all()
