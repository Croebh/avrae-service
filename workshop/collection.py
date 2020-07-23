import abc
import collections
import datetime
import enum

from bson import ObjectId
from flask import current_app

from lib.errors import NotAllowed
from workshop.errors import CollectableNotFound, CollectionNotFound
from workshop.utils import EditorMixin, GuildActiveMixin, SubscriberMixin


class PublicationState(enum.Enum):
    PRIVATE = 'PRIVATE'
    UNLISTED = 'UNLISTED'
    PUBLISHED = 'PUBLISHED'


class WorkshopCollection(SubscriberMixin, GuildActiveMixin, EditorMixin):
    def __init__(self,
                 _id, name, description, image, owner,
                 alias_ids, snippet_ids,
                 publish_state, num_subscribers, num_guild_subscribers, last_edited, created_at, tags):
        """
        :param _id: The MongoDB ID of this collection.
        :type _id: bson.ObjectId
        :param name: The name of this collection.
        :type name: str
        :param description: The description.
        :type description: str
        :param image: The URL to the image for this collection, if applicable.
        :type image: str or None
        :param owner: The owner ID of this collection.
        :type owner: int
        :param alias_ids: A list of alias IDs contained in this collection.
        :type alias_ids: list[ObjectId]
        :param snippet_ids: A list of snippet IDs contained in this collection.
        :type snippet_ids: list[ObjectId]
        :param publish_state: The publication state of this collection.
        :type publish_state: PublicationState
        :param num_subscribers: The approximate number of subscribers of this collection.
        :type num_subscribers: int
        :param num_guild_subscribers: The approximate number of guilds subscribed to this collection.
        :type num_guild_subscribers: int
        :param last_edited: The time this collection was last edited.
        :type last_edited: datetime.datetime
        :param created_at: The time this collection was created.
        :type created_at: datetime.datetime
        :param tags: The tags of this collection
        :type tags: list[str]
        """
        super().__init__(_id)
        self.name = name
        self.description = description
        self.image = image
        self.owner = owner
        self._aliases = None
        self._snippets = None
        self.publish_state = publish_state
        self.approx_num_subscribers = num_subscribers
        self.approx_num_guild_subscribers = num_guild_subscribers
        self.last_edited = last_edited
        self.created_at = created_at
        self.tags = tags
        # lazy-load aliases/snippets
        self._alias_ids = alias_ids
        self._snippet_ids = snippet_ids

    @property
    def url(self):
        return f"https://avrae.io/dashboard/workshop/{self.id}"

    @property
    def aliases(self):
        if self._aliases is None:
            raise AttributeError("Aliases are not loaded yet - run load_aliases() first")
        return self._aliases

    @property
    def snippets(self):
        if self._snippets is None:
            raise AttributeError("Snippets are not loaded yet - run load_snippets() first")
        return self._snippets

    def load_aliases(self):
        self._aliases = []
        for alias_id in self._alias_ids:
            self._aliases.append(WorkshopAlias.from_id(alias_id, collection=self, parent=None))
        return self._aliases

    def load_snippets(self):
        self._snippets = []
        for snippet_id in self._snippet_ids:
            self._snippets.append(WorkshopSnippet.from_id(snippet_id, collection=self))
        return self._snippets

    # constructors
    @classmethod
    def from_id(cls, _id):
        if not isinstance(_id, ObjectId):
            _id = ObjectId(_id)

        raw = current_app.mdb.workshop_collections.find_one({"_id": _id})
        if raw is None:
            raise CollectionNotFound()

        return cls(raw['_id'], raw['name'], raw['description'], raw['image'], raw['owner'],
                   raw['alias_ids'], raw['snippet_ids'],
                   PublicationState(raw['publish_state']), raw['num_subscribers'], raw['num_guild_subscribers'],
                   raw['last_edited'], raw['created_at'], raw['tags'])

    # helpers
    @classmethod
    def user_owned_ids(cls, user_id: int):
        """Returns an iterator of ObjectIds of objects the contextual user owns."""
        for obj in current_app.mdb.workshop_collections.find({"owner": user_id}, ['_id']):
            yield obj['_id']

    @classmethod
    def user_subscribed(cls, user_id: int):
        """Returns an iterator of WorkshopCollections that the user has subscribed to."""
        for coll_id in cls.my_sub_ids(user_id):
            try:
                yield cls.from_id(coll_id)
            except CollectionNotFound:
                continue

    @classmethod
    def server_subscribed(cls, guild_id: int):
        """Returns an generator of WorkshopCollections that the server has subscribed to."""
        for coll_id in cls.guild_active_ids(guild_id):
            try:
                yield cls.from_id(coll_id)
            except CollectionNotFound:
                continue

    def is_owner(self, user_id: int):
        return user_id == self.owner

    def to_dict(self, js=False):
        out = {
            "name": self.name, "description": self.description, "image": self.image, "owner": self.owner,
            "alias_ids": self._alias_ids, "snippet_ids": self._snippet_ids,
            "publish_state": self.publish_state.value, "num_subscribers": self.approx_num_subscribers,
            "num_guild_subscribers": self.approx_num_guild_subscribers, "last_edited": self.last_edited,
            "created_at": self.created_at, "tags": self.tags
        }
        if js:
            out['owner'] = str(self.owner)
            out['_id'] = self.id
        return out

    # database operations
    @classmethod
    def create_new(cls, user_id: int, name, description, image):
        """Inserts a new collection into the database and returns the new collection."""
        now = datetime.datetime.now()
        # noinspection PyTypeChecker
        # id is None until inserted
        inst = cls(None, name, description, image, user_id, [], [], PublicationState.PRIVATE, 0, 0, now, now, [])
        result = current_app.mdb.workshop_collections.insert_one(inst.to_dict())
        inst.id = result.inserted_id
        return inst

    def update_info(self, name: str, description: str, image):
        """Updates the collection's user information."""
        current_app.mdb.workshop_collections.update_one(
            {"_id": self.id},
            {
                "$set": {"name": name, "description": description, "image": image},
                "$currentDate": {"last_edited": True}
            }
        )
        self.name = name
        self.description = description
        self.image = image
        self.last_edited = datetime.datetime.now()

    # bindings
    def _generate_default_alias_bindings(self):
        """
        Returns a list of {name: str, id: ObjectId} bindings based on the default names of aliases in the collection.
        """
        if self._aliases is None:
            self.load_aliases()
        return [{"name": alias.name, "id": alias.id} for alias in self._aliases]

    def _generate_default_snippet_bindings(self):
        """
        Returns a list of {name: str, id: ObjectId} bindings based on the default names of snippets in the collection.
        """
        if self._snippets is None:
            self.load_snippets()
        return [{"name": snippet.name, "id": snippet.id} for snippet in self._snippets]

    def _bindings_sanity_check(self, the_ids, the_bindings, binding_cls):
        # sanity check: ensure all aliases are in the bindings
        binding_ids = {b['id'] for b in the_bindings}
        missing_ids = set(the_ids).difference(binding_ids)
        for missing in missing_ids:
            obj = binding_cls.from_id(missing, collection=self)
            the_bindings.append({"name": obj.name, "id": obj.id})

        # sanity check: ensure there is no binding to anything deleted
        return [b for b in the_bindings if b['id'] in the_ids]

    def update_alias_bindings(self, subscription_doc):
        """Updates the alias bindings for a given subscription (given the entire subscription document)."""
        the_bindings = self._bindings_sanity_check(self._alias_ids, subscription_doc['alias_bindings'], WorkshopAlias)

        self.sub_coll(current_app.mdb).update_one(
            {"_id": subscription_doc['_id']},
            {"$set": {"alias_bindings": the_bindings}}
        )

    def update_snippet_bindings(self, subscription_doc):
        """Updates the snippet bindings for a given subscription (given the entire subscription document)."""
        the_bindings = self._bindings_sanity_check(self._snippet_ids, subscription_doc['snippet_bindings'],
                                                   WorkshopSnippet)

        self.sub_coll(current_app.mdb).update_one(
            {"_id": subscription_doc['_id']},
            {"$set": {"snippet_bindings": the_bindings}}
        )

    # implementations
    @staticmethod
    def sub_coll(mdb):
        return mdb.workshop_subscriptions

    def subscribe(self, user_id: int):
        """Adds the contextual author as a subscriber, with default name bindings."""
        if self.is_subscribed(user_id):
            raise NotAllowed("You are already subscribed to this.")
        if self.publish_state == PublicationState.PRIVATE and not self.is_owner(user_id):
            raise NotAllowed("This collection is private.")

        # generate default bindings
        alias_bindings = self._generate_default_alias_bindings()
        snippet_bindings = self._generate_default_snippet_bindings()

        # insert subscription
        self.sub_coll(current_app.mdb).insert_one(
            {"type": "subscribe", "subscriber_id": user_id, "object_id": self.id,
             "alias_bindings": alias_bindings, "snippet_bindings": snippet_bindings}
        )
        # increase subscription count
        current_app.mdb.workshop_collections.update_one(
            {"_id": self.id},
            {"$inc": {"num_subscribers": 1}}
        )
        # log subscribe event
        current_app.mdb.analytics_alias_events.insert_one(
            {"type": "subscribe", "object_id": self.id, "timestamp": datetime.datetime.utcnow()}
        )

    def unsubscribe(self, user_id: int):
        # remove sub doc
        super().unsubscribe(user_id)
        # decr sub count
        current_app.mdb.workshop_collections.update_one(
            {"_id": self.id},
            {"$inc": {"num_subscribers": -1}}
        )
        # log unsub event
        current_app.mdb.analytics_alias_events.insert_one(
            {"type": "unsubscribe", "object_id": self.id, "timestamp": datetime.datetime.utcnow()}
        )

    def set_server_active(self, guild_id: int):
        """Sets the object as active for the contextual guild, with default name bindings."""
        if self.is_server_active(guild_id):
            raise NotAllowed("This collection is already installed on this server.")
        if self.publish_state == PublicationState.PRIVATE:
            raise NotAllowed("This collection is private.")

        # generate default bindings
        alias_bindings = self._generate_default_alias_bindings()
        snippet_bindings = self._generate_default_snippet_bindings()

        # insert sub doc
        self.sub_coll(current_app.mdb).insert_one(
            {"type": "server_active", "subscriber_id": guild_id, "object_id": self.id,
             "alias_bindings": alias_bindings, "snippet_bindings": snippet_bindings}
        )
        # incr sub count
        current_app.mdb.workshop_collections.update_one(
            {"_id": self.id},
            {"$inc": {"num_guild_subscribers": 1}}
        )
        # log sub event
        current_app.mdb.analytics_alias_events.insert_one(
            {"type": "server_subscribe", "object_id": self.id, "timestamp": datetime.datetime.utcnow()}
        )

    def unset_server_active(self, guild_id: int):
        # remove sub doc
        super().unset_server_active(guild_id)
        # decr sub count
        current_app.mdb.workshop_collections.update_one(
            {"_id": self.id},
            {"$inc": {"num_guild_subscribers": -1}}
        )
        # log unsub event
        current_app.mdb.analytics_alias_events.insert_one(
            {"type": "server_unsubscribe", "object_id": self.id, "timestamp": datetime.datetime.utcnow()}
        )


class WorkshopCollectableObject(abc.ABC):
    def __init__(self, _id, name,
                 code, versions, docs, entitlements, collection_id,
                 collection=None):
        """
        :param _id: The MongoDB ID of this object.
        :type _id: bson.ObjectId
        :param name: The name of this object.
        :type name: str
        :param code: The code of this object.
        :type code: str
        :param versions: A list of code versions of this object.
        :type versions: list[CodeVersion]
        :param docs: The help docs of this object.
        :type docs: str
        :param entitlements: A list of entitlements required to run this.
        :type entitlements: list[RequiredEntitlement]
        :param collection_id: The ID of the top-level Collection this object is a member of.
        :type collection_id: ObjectId
        :param collection: The top-level Collection this object is a member of.
        :type collection: WorkshopCollection
        """
        self.id = _id
        self.name = name
        self.code = code
        self.versions = versions
        self.docs = docs
        self.entitlements = entitlements
        self._collection = collection
        # lazy-load collection
        self._collection_id = collection_id

    @property
    def short_docs(self):
        return self.docs.split('\n')[0]

    @property
    def collection(self):
        if self._collection is None:
            raise AttributeError("Collection is not loaded - run load_collection() first")
        return self._collection

    def load_collection(self):
        self._collection = WorkshopCollection.from_id(self._collection_id)
        return self._collection

    def get_entitlements(self):
        """Returns a dict of {entity_type: [entity_id]} for required entitlements."""
        out = collections.defaultdict(lambda: [])
        for ent in self.entitlements:
            out[ent.entity_type].append(ent.entity_id)
        return out


class WorkshopAlias(WorkshopCollectableObject):
    def __init__(self, _id, name, code, versions, docs, entitlements, collection_id, subcommand_ids, parent_id,
                 collection=None, parent=None):
        """
        :param subcommand_ids: The alias IDs that are a child of this alias.
        :type subcommand_ids: list[ObjectId]
        :param parent: The alias that is a parent of this alias, if applicable.
        :type parent: WorkshopAlias or None
        """
        super().__init__(_id, name, code, versions, docs, entitlements,
                         collection_id=collection_id, collection=collection)
        self._subcommands = None
        self._parent = parent
        # lazy-load subcommands, collection, parent
        self._subcommand_ids = subcommand_ids
        self._parent_id = parent_id

    @property
    def parent(self):
        if self._parent is None:
            raise AttributeError("Parent is not loaded yet - run load_parent() first")
        return self._parent

    @property
    def subcommands(self):
        if self._subcommands is None:
            raise AttributeError("Subcommands are not loaded yet - run load_subcommands() first")
        return self._subcommands

    def load_parent(self):
        self._parent = WorkshopAlias.from_id(self._parent_id, collection=self._collection)
        return self._parent

    def load_subcommands(self):
        self._subcommands = []
        for subcommand_id in self._subcommand_ids:
            self._subcommands.append(
                WorkshopAlias.from_id(subcommand_id, collection=self._collection, parent=self))
        return self._subcommands

    # constructors
    @classmethod
    def from_dict(cls, raw, collection=None, parent=None):
        versions = [CodeVersion.from_dict(cv) for cv in raw['versions']]
        entitlements = [RequiredEntitlement.from_dict(ent) for ent in raw['entitlements']]
        return cls(raw['_id'], raw['name'], raw['code'], versions, raw['docs'], entitlements, raw['collection_id'],
                   raw['subcommand_ids'], raw['parent_id'], collection, parent)

    @classmethod
    def from_id(cls, _id, collection=None, parent=None):
        if not isinstance(_id, ObjectId):
            _id = ObjectId(_id)

        raw = current_app.mdb.workshop_aliases.find_one({"_id": _id})
        if raw is None:
            raise CollectableNotFound()
        return cls.from_dict(raw, collection, parent)


class WorkshopSnippet(WorkshopCollectableObject):
    @classmethod
    def from_id(cls, _id, collection=None):
        if not isinstance(_id, ObjectId):
            _id = ObjectId(_id)

        raw = current_app.mdb.workshop_snippets.find_one({"_id": _id})
        if raw is None:
            raise CollectableNotFound()

        versions = [CodeVersion.from_dict(cv) for cv in raw['versions']]
        entitlements = [RequiredEntitlement.from_dict(ent) for ent in raw['entitlements']]
        return cls(raw['_id'], raw['name'], raw['code'], versions, raw['docs'], entitlements,
                   raw['collection_id'], collection)


class CodeVersion:
    def __init__(self, version, content, created_at, is_current):
        """
        :param version: The version of code.
        :type version: int
        :param content: The content of this version.
        :type content: str
        :param created_at: The time this version was created.
        :type created_at: datetime.datetime
        :param is_current: Whether this version is the current live version.
        :type is_current: bool
        """
        self.version = version
        self.content = content
        self.created_at = created_at
        self.is_current = is_current

    @classmethod
    def from_dict(cls, raw):
        return cls(**raw)


class RequiredEntitlement:
    """An entitlement that a user must have to invoke this alias/snippet."""

    def __init__(self, entity_type, entity_id, required=False):
        """
        :param str entity_type: The entity type of the required entitlement.
        :param int entity_id: The entity id of the required entitlement.
        :param bool required: Whether this entitlement was required by a moderator and cannot be removed.
        """
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.required = required

    @classmethod
    def from_dict(cls, raw):
        return cls(**raw)
